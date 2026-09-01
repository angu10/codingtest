"""Run directories and automatic failure bundles.

The bundle is written on *every* non-success terminal state, not when someone remembers to ask for
it. Its contents are exactly what a debugging engineer opens first: what was expected, what was
observed, every locator that was tried and why each was rejected, a screenshot at the moment of
failure, the DOM, and the event log up to that point.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from interface_cua.observability.events import (
    DEFAULT_SENSITIVE_FIELDS,
    EventLog,
    Notice,
    NoticeKind,
    Observation,
    RunEvent,
)
from interface_cua.policy.redaction import Redactor
from interface_cua.schema.result import ReplayResult
from interface_cua.surface.base import SurfaceAdapter

FAILURE_BUNDLE = "failure"


class EvidenceWriter:
    """Owns one run directory: `<root>/<run_id>/`.

    `run_id` is an operator-supplied *label*, so it is deliberately not redacted — masking it would
    break correlation between the directory, the log lines and the bundle. That leaves one residual
    egress path worth naming: an operator who names a run after member data puts it on disk in the
    one field redaction does not touch. The CLI's default (`replay_<timestamp>_<capability>`)
    cannot carry it.
    """

    def __init__(self, root: Path | str, run_id: str, redactor: Redactor | None = None) -> None:
        self.run_id = run_id
        self.dir = Path(root) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor or Redactor(DEFAULT_SENSITIVE_FIELDS)
        self.events = EventLog(self.dir / "events.jsonl", run_id, self.redactor)

    def emit(self, event: RunEvent | Notice) -> None:
        self.events.emit(event)

    def notice(self, kind: NoticeKind, **detail: Any) -> None:
        self.events.emit(Notice(run_id=self.run_id, kind=kind, detail=detail))

    async def observe(self, surface: SurfaceAdapter, *, screenshot_as: str | None = None) -> Observation:
        """Capture the current surface state for an event."""

        screenshot = None
        if screenshot_as is not None:
            screenshot = await self._screenshot(surface, screenshot_as)
        return Observation(url=surface.current_url, screenshot=screenshot)

    async def capture_failure(
        self, surface: SurfaceAdapter, result: ReplayResult
    ) -> Path | None:
        """Write the failure bundle. Returns the bundle directory."""

        if result.status == "success":
            return None
        bundle = self.dir / FAILURE_BUNDLE
        bundle.mkdir(parents=True, exist_ok=True)

        summary: dict[str, Any] = self.redactor.redact(
            result.model_dump(mode="json", exclude_none=True)
        ).value
        (bundle / "result.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        # Best-effort: a surface that cannot screenshot or snapshot must not turn a diagnosable
        # failure into an undiagnosable crash.
        try:
            payload = await surface.screenshot()
            (bundle / "screenshot.png").write_bytes(payload)
        except Exception as exc:  # noqa: BLE001
            (bundle / "screenshot.error.txt").write_text(str(exc), encoding="utf-8")
        try:
            (bundle / "dom_snapshot.html").write_text(
                await surface.dom_snapshot(), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            (bundle / "dom_snapshot.error.txt").write_text(str(exc), encoding="utf-8")

        trace = getattr(surface, "save_trace", None)
        if trace is not None:
            try:
                await trace(bundle / "trace.zip")
            except Exception as exc:  # noqa: BLE001
                (bundle / "trace.error.txt").write_text(str(exc), encoding="utf-8")
        return bundle

    async def _screenshot(self, surface: SurfaceAdapter, name: str) -> str | None:
        shots = self.dir / "screenshots"
        shots.mkdir(parents=True, exist_ok=True)
        try:
            (shots / name).write_bytes(await surface.screenshot())
        except Exception:  # noqa: BLE001
            return None
        return f"screenshots/{name}"
