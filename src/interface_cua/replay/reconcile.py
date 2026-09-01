"""Resolving an ambiguous write without repeating it.

A submit that returns a 500 has told you nothing about whether it worked. The tempting response —
retry — is the one that creates two accounts. Invariant 4 says never re-issue a mutating action
whose effect is uncertain, so this module does the only other thing available: look somewhere else.

The probe is declared by the artifact and is read-only. It never touches the route that failed, and
it never touches the Create control again. There are exactly three answers, and "I could not tell"
is one of them — reconciliation that cannot fail is reconciliation you cannot trust.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from interface_cua.observability.events import NoticeKind
from interface_cua.observability.evidence import EvidenceWriter
from interface_cua.policy.engine import AuthorizationContext, PolicyEngine, PolicyVerdict
from interface_cua.replay.waits import ConditionChecker
from interface_cua.schema.artifact import (
    ActionSpec,
    InputValue,
    ReconciliationSpec,
    RiskLevel,
    RiskSpec,
)
from interface_cua.schema.result import (
    FailureCategory,
    FailureResult,
    NeedsHumanResult,
    ReplayResult,
    SuccessResult,
    UnknownSideEffectResult,
)
from interface_cua.surface.base import SurfaceAdapter


class Reconciler:
    """Turns `unknown_side_effect` into an answer, or into an honest escalation."""

    def __init__(
        self,
        surface: SurfaceAdapter,
        checker: ConditionChecker,
        policy: PolicyEngine,
        *,
        timeout_ms: int = 10_000,
        evidence: EvidenceWriter | None = None,
    ) -> None:
        self.surface = surface
        self.checker = checker
        self.policy = policy
        self.timeout_ms = timeout_ms
        self.evidence = evidence

    def _notice(self, kind: NoticeKind, **detail: Any) -> None:
        if self.evidence is not None:
            self.evidence.notice(kind, **detail)

    async def resolve(
        self,
        spec: ReconciliationSpec,
        pending: UnknownSideEffectResult,
        inputs: dict[str, object],
        outputs: dict[str, Any],
    ) -> ReplayResult:
        probe_url = _probe_url(spec, inputs, self.surface.current_url)
        self._notice(NoticeKind.RECONCILIATION_STARTED, step=pending.step, probe=spec.probe_route)

        # The probe is a navigation like any other, so it goes through the same `authorize()`.
        # A probe that could wander off the allowlist would be a hole, not a safety net.
        decision = self.policy.authorize(
            ActionSpec(type="navigate", value=InputValue(from_input="${inputs.probe}")),
            RiskSpec(level=RiskLevel.READ),
            AuthorizationContext(self.surface.current_url, destination_url=probe_url),
        )
        if decision.verdict is not PolicyVerdict.ALLOW:
            self._notice(
                NoticeKind.RECONCILIATION_INCONCLUSIVE, step=pending.step, error=decision.rule
            )
            return NeedsHumanResult(reason="RECONCILIATION_INCONCLUSIVE", step=pending.step)

        try:
            await self.surface.navigate(probe_url)
            await self.surface.wait_until_settled(self.timeout_ms)
            landed = await self.checker.wait_for(spec.landed, inputs, self.timeout_ms)
        except Exception as exc:  # noqa: BLE001
            # The probe itself failed. We now know strictly less than nothing about the write,
            # and guessing in either direction is worse than saying so.
            self._notice(
                NoticeKind.RECONCILIATION_INCONCLUSIVE,
                step=pending.step,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )
            return NeedsHumanResult(
                reason="RECONCILIATION_INCONCLUSIVE",
                step=pending.step,
            )

        if landed:
            # The write did happen; the failure was only in the response. Reported as success and
            # flagged, so a caller can tell a clean run from a recovered one.
            self._notice(NoticeKind.RECONCILIATION_CONFIRMED, step=pending.step)
            return SuccessResult(outputs=outputs, reconciled=True)

        # The write did not land. Nothing was created, so the whole capability is safe to run
        # again — which is a very different thing from re-clicking Create on this session.
        self._notice(NoticeKind.RECONCILIATION_ABSENT, step=pending.step)
        return FailureResult(
            category=FailureCategory.APPLICATION_ERROR,
            retryable=True,
            step=pending.step,
            expected="the submitted record to exist after an ambiguous response",
            observed="an independent read-only probe found no such record; the write did not land",
        )


def _probe_url(spec: ReconciliationSpec, inputs: dict[str, object], base: str) -> str:
    """Fill the probe route's parameters from declared inputs."""

    path = spec.probe_route
    for parameter, reference in spec.bindings.items():
        name = reference.removeprefix("${inputs.").removesuffix("}")
        path = path.replace(f":{parameter}", str(inputs[name]))
    return urljoin(base, path)
