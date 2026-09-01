"""Structured run events — "what the agent did, and why".

One JSON object per line. Discovery and replay emit the *same* shape, which is the point: the only
structural difference is `decision_source`, and the schema forbids a model rationale on an event
whose decisions came from an artifact. Invariant 1 is therefore visible in the evidence itself
rather than merely asserted in prose.

Every write goes through `Redactor` before it reaches disk, so the log is an egress point that is
masked by construction rather than by remembering.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from interface_cua.policy.redaction import Redactor

DecisionSource = Literal["model", "artifact"]

#: Field names masked by construction wherever they appear in an event payload. This is the
#: deterministic stage-1 control: it cannot miss a field it was told about, and it needs no model.
DEFAULT_SENSITIVE_FIELDS = frozenset(
    {
        "member_id",
        "account_id",
        "account_number",
        "ssn",
        "date_of_birth",
        "dob",
        "phone",
        "password",
        "secret",
        "token",
    }
)


class StrictEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Observation(StrictEvent):
    """What the surface looked like when the decision was taken."""

    url: str
    screenshot: str | None = None
    page_digest: str | None = None


class ProposedAction(StrictEvent):
    """The action about to be attempted.

    `value_ref` holds `${inputs.member_id}`, never the value itself — the same rule the artifact
    follows (invariant 7). Coordinates appear only on discovery events; replay is semantic.
    """

    type: str
    x: int | None = None
    y: int | None = None
    value_ref: str | None = None


class PolicyDecisionEvent(StrictEvent):
    verdict: str
    rule: str
    origin_ok: bool


class ContentScanEvent(StrictEvent):
    verdict: str
    scanner: str
    signals: list[str] = Field(default_factory=list)


class TargetEvent(StrictEvent):
    frame: str | None = None
    strategy: dict[str, Any] | None = None
    unique: bool = False
    attempts: list[dict[str, Any]] = Field(default_factory=list)


class StepResult(StrictEvent):
    ok: bool
    elapsed_ms: int = Field(ge=0)
    postcondition: str | None = None
    outcome: str | None = None


class RunEvent(StrictEvent):
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    run_id: str = Field(min_length=1)
    step_index: int = Field(ge=0)
    step_id: str | None = None
    decision_source: DecisionSource
    observation: Observation
    proposed_action: ProposedAction | None = None
    policy_decision: PolicyDecisionEvent | None = None
    target: TargetEvent | None = None
    result: StepResult | None = None
    #: The model's own summary of why it acted. Discovery only, by construction.
    rationale_summary: str | None = None
    #: Untrusted page text is scanned before it enters model context. Discovery only.
    content_scan: ContentScanEvent | None = None

    @model_validator(mode="after")
    def artifact_decisions_carry_no_model_signals(self) -> RunEvent:
        if self.decision_source == "artifact":
            if self.rationale_summary is not None:
                raise ValueError("replay events cannot carry a model rationale")
            if self.content_scan is not None:
                raise ValueError("replay events cannot carry a model-context content scan")
        return self


class NoticeKind(StrEnum):
    """Operational signals that are not step outcomes."""

    REDACTION_SCHEMA_GAP = "REDACTION_SCHEMA_GAP"
    INTERSTITIAL_DISMISSED = "INTERSTITIAL_DISMISSED"
    STEP_RETRIED = "STEP_RETRIED"
    #: Untrusted page text tripped the scanner *before* it entered model context (discovery only).
    CONTENT_RISK_FLAGGED = "CONTENT_RISK_FLAGGED"
    #: A model coordinate landed outside any control and was snapped to the nearest one.
    CLICK_SNAPPED = "CLICK_SNAPPED"


class Notice(StrictEvent):
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    run_id: str = Field(min_length=1)
    kind: NoticeKind
    detail: dict[str, Any] = Field(default_factory=dict)


class EventLog:
    """Append-only JSONL sink that redacts before writing."""

    def __init__(self, path: Path, run_id: str, redactor: Redactor | None = None) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.redactor = redactor or Redactor(DEFAULT_SENSITIVE_FIELDS)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: RunEvent | Notice) -> None:
        payload = event.model_dump(mode="json", exclude_none=True)
        redaction = self.redactor.redact(payload)
        self._write(redaction.value)
        if redaction.schema_gaps:
            # A stage-2 hit means the *field schema* is incomplete, so this is a coverage signal
            # about our own redaction rather than a quiet cleanup. See plan §9.
            self._write(
                Notice(
                    run_id=self.run_id,
                    kind=NoticeKind.REDACTION_SCHEMA_GAP,
                    detail={"kinds": list(redaction.schema_gaps)},
                ).model_dump(mode="json")
            )

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [
            json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line
        ]

    def _write(self, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
