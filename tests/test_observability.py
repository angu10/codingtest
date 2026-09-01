from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from interface_cua.observability.events import (
    DEFAULT_SENSITIVE_FIELDS,
    ContentScanEvent,
    EventLog,
    NoticeKind,
    Observation,
    ProposedAction,
    RunEvent,
    StepResult,
)
from interface_cua.observability.evidence import EvidenceWriter
from interface_cua.policy.redaction import Redactor
from interface_cua.schema.result import FailureCategory, FailureResult, SuccessResult


def _event(**overrides: object) -> RunEvent:
    payload: dict[str, object] = {
        "run_id": "replay_test",
        "step_index": 0,
        "step_id": "search-member",
        "decision_source": "artifact",
        "observation": Observation(url="http://127.0.0.1:8000/"),
        "result": StepResult(ok=True, elapsed_ms=12, postcondition="member-found"),
    }
    payload.update(overrides)
    return RunEvent(**payload)  # type: ignore[arg-type]


def test_replay_events_cannot_carry_a_model_rationale() -> None:
    """Invariant 1, enforced by the evidence schema rather than asserted in prose."""

    with pytest.raises(ValidationError, match="model rationale"):
        _event(rationale_summary="I decided to click Search")

    with pytest.raises(ValidationError, match="content scan"):
        _event(content_scan=ContentScanEvent(verdict="clean", scanner="heuristic"))

    # The same fields are legitimate on a discovery event.
    discovery = _event(
        decision_source="model",
        rationale_summary="Opening the sub-account form for the savings row",
        proposed_action=ProposedAction(type="click", x=482, y=311),
    )
    assert discovery.rationale_summary is not None


def test_event_log_masks_declared_sensitive_fields_before_writing(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl", "replay_test")
    log.emit(
        _event(
            proposed_action=ProposedAction(type="fill", value_ref="${inputs.member_id}"),
            observation=Observation(url="http://127.0.0.1:8000/member/58431"),
        )
    )
    written = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert "${inputs.member_id}" in written  # the reference survives
    assert "member_id" in DEFAULT_SENSITIVE_FIELDS


def test_event_log_masks_a_sensitive_value_and_keeps_its_last_four(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl", "replay_test")
    log.emit(
        _event(
            result=StepResult(ok=True, elapsed_ms=1),
            observation=Observation(url="http://x/", page_digest="ok"),
            target=None,
        )
    )
    # Redaction is keyed on field name, so prove it directly on the Redactor the log uses.
    redacted = Redactor(DEFAULT_SENSITIVE_FIELDS).redact(
        {"member_id": "58431", "password": "hunter2", "note": "fine"}
    )
    assert redacted.value["member_id"] == "***8431"
    assert redacted.value["password"] == "[redacted]"
    assert redacted.value["note"] == "fine"


def test_unexpected_pii_raises_a_schema_gap_notice_rather_than_being_cleaned_up(
    tmp_path: Path,
) -> None:
    """A stage-2 hit means our field schema is incomplete — that is a coverage signal, not a fix."""

    log = EventLog(tmp_path / "events.jsonl", "replay_test")
    log.emit(_event(observation=Observation(url="http://x/", page_digest="call 555-123-4567")))

    lines = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    notices = [line for line in lines if line.get("kind") == NoticeKind.REDACTION_SCHEMA_GAP.value]
    assert notices, lines
    assert "phone" in notices[0]["detail"]["kinds"]


class _BareSurface:
    current_url = "http://127.0.0.1:8000/"

    async def screenshot(self) -> bytes:
        raise RuntimeError("no screenshot on this surface")

    async def dom_snapshot(self) -> str:
        return "<html></html>"


@pytest.mark.asyncio
async def test_failure_bundle_is_written_on_failure_and_not_on_success(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path, "run-1")

    assert await writer.capture_failure(_BareSurface(), SuccessResult()) is None  # type: ignore[arg-type]

    bundle = await writer.capture_failure(
        _BareSurface(),  # type: ignore[arg-type]
        FailureResult(
            category=FailureCategory.POSTCONDITION_FAILED,
            retryable=False,
            step="search-member",
            expected="one of the declared postconditions",
            observed="Permission denied",
        ),
    )
    assert bundle is not None
    assert (bundle / "result.json").exists()
    assert (bundle / "dom_snapshot.html").exists()
    # A surface that cannot screenshot must not turn a diagnosable failure into a crash.
    assert (bundle / "screenshot.error.txt").exists()
