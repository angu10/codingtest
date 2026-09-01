"""Policy enforcement shared by discovery and deterministic replay."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatch
from urllib.parse import ParseResult, urlparse

from interface_cua.schema.artifact import ActionSpec, RiskLevel, RiskSpec


class PolicyVerdict(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_HUMAN = "REQUIRE_HUMAN"


@dataclass(frozen=True, slots=True)
class AuthorizationContext:
    current_url: str
    human_confirmed: bool = False
    #: Absolute URL a navigate action would land on. Checking only ``current_url`` would authorize
    #: where the session already is and let the action walk straight off the allowlist.
    destination_url: str | None = None
    #: Set when the value an ``extract`` would record looks like regulated data. The caller scans;
    #: policy decides. Reading data out is an action like any other, and it is the only one whose
    #: risk depends on what is on the screen rather than on what the step is.
    sensitive_extraction: bool = False


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    verdict: PolicyVerdict
    rule: str
    origin_ok: bool


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    allowed_origins: frozenset[str]
    allowed_action_types: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"click", "fill", "select", "keypress", "navigate", "extract"}
        )
    )
    blocked_route_patterns: tuple[str, ...] = ()
    #: Whether a capability may record a value that looks like regulated data. Fail-closed: a
    #: capability that legitimately needs an SSN is authored by someone who turned this on
    #: deliberately, and that decision lives in configuration where it can be reviewed.
    allow_sensitive_extraction: bool = False


class PolicyEngine:
    """Fail-closed allowlist and per-step risk policy."""

    def __init__(self, config: PolicyConfig) -> None:
        self.config = config

    def authorize(
        self,
        action: ActionSpec,
        risk: RiskSpec,
        context: AuthorizationContext,
    ) -> PolicyDecision:
        parsed = urlparse(context.current_url)
        if not self._origin_allowed(parsed):
            return PolicyDecision(PolicyVerdict.DENY, "origin:not_allowed", False)
        if action.type not in self.config.allowed_action_types:
            return PolicyDecision(PolicyVerdict.DENY, "action:not_allowed", True)

        checked = [parsed]
        if context.destination_url is not None:
            destination = urlparse(context.destination_url)
            if not self._origin_allowed(destination):
                return PolicyDecision(PolicyVerdict.DENY, "origin:destination_not_allowed", False)
            checked.append(destination)
        if any(
            fnmatch(url.path, pattern)
            for url in checked
            for pattern in self.config.blocked_route_patterns
        ):
            return PolicyDecision(PolicyVerdict.DENY, "route:blocked", True)
        # Data egress. Every other action is judged by what it does; this one is judged by what it
        # would carry out of the session, which is why the caller has to scan first.
        if (
            context.sensitive_extraction
            and not self.config.allow_sensitive_extraction
        ):
            return PolicyDecision(PolicyVerdict.DENY, "extraction:sensitive_value", True)
        if not context.human_confirmed:
            # `requires_confirmation` is load-bearing, not decorative: a step may demand a human
            # at any risk level. The schema forces it on for consequential writes; this honours it
            # wherever an author set it.
            if risk.level == RiskLevel.CONSEQUENTIAL_WRITE:
                return PolicyDecision(PolicyVerdict.REQUIRE_HUMAN, "risk:consequential_write", True)
            if risk.requires_confirmation:
                return PolicyDecision(PolicyVerdict.REQUIRE_HUMAN, "risk:requires_confirmation", True)
        return PolicyDecision(PolicyVerdict.ALLOW, f"risk:{risk.level.value}", True)

    def _origin_allowed(self, url: ParseResult) -> bool:
        return f"{url.scheme}://{url.netloc}" in self.config.allowed_origins

