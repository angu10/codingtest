"""What an operator is handed when replay stops, and the state machine around it.

The lease is the safety property: exactly one controller owns the session, and every surface
mutation asserts the caller holds it. `HandoffCoordinator` is what drives that lease through the
transitions in plan §10 while a human takes over the *same live browser* — same window, same
cookies, same session.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from interface_cua.handoff.human_recorder import HumanAction, HumanActionRecorder
from interface_cua.handoff.lease import Controller, LeaseState, SessionLease
from interface_cua.observability.evidence import EvidenceWriter


@dataclass(slots=True)
class InterventionRequest:
    """Everything an operator needs to decide, without reading the code."""

    run_id: str
    capability_id: str
    step_id: str
    reason: str
    url: str
    raised_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    before_screenshot: str | None = None
    after_screenshot: str | None = None
    human_actions: list[HumanAction] = field(default_factory=list)
    resolution: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "capability_id": self.capability_id,
            "step_id": self.step_id,
            "reason": self.reason,
            "url": self.url,
            "raised_at": self.raised_at.isoformat(),
            "resolution": self.resolution,
            "human_actions": [action.describe() for action in self.human_actions],
        }


class HandoffCoordinator:
    """Owns the lease while a human is in the loop.

    Nothing here touches the page on the human's behalf. Its whole job is to make sure automation
    has genuinely stopped before the human starts, and that the human has genuinely finished
    before automation resumes — with the lease as the single source of truth about who may act.
    """

    def __init__(
        self,
        *,
        lease: SessionLease,
        surface: Any,
        page: Any,
        evidence: EvidenceWriter,
        sensitive_fields: frozenset[str] = frozenset(),
    ) -> None:
        self.lease = lease
        self.surface = surface
        self.evidence = evidence
        self.recorder = HumanActionRecorder(page, sensitive_fields)
        self.request: InterventionRequest | None = None
        #: Set when the operator decides. The run waits on this rather than polling.
        self.decided: asyncio.Event = asyncio.Event()
        self.decision: str | None = None

    async def raise_request(
        self, *, run_id: str, capability_id: str, step_id: str, reason: str
    ) -> InterventionRequest:
        """Pause automation, hand the lease to the human, and start recording them."""

        self.lease.request_pause()
        self.lease.mark_automation_paused()

        request = InterventionRequest(
            run_id=run_id,
            capability_id=capability_id,
            step_id=step_id,
            reason=reason,
            url=self.surface.current_url,
            before_screenshot=await self._shot("before-handoff.png"),
        )
        # Arm the listener *before* the human can touch anything.
        await self.recorder.install()
        self.lease.grant_human_control()

        self.request = request
        self.decided.clear()
        self.decision = None
        return request

    async def resolve(self, decision: str) -> InterventionRequest:
        """The operator chose. Take the lease back and record what they did."""

        request = self.request
        if request is None:
            raise RuntimeError("no intervention is open")

        request.human_actions = await self.recorder.drain()
        request.after_screenshot = await self._shot("after-handoff.png")
        request.resolution = decision

        self.lease.release_human_control()
        # RESUME_VALIDATION is entered here but *not* left: whether automation may continue is
        # decided by re-reading the step's precondition, not by the operator clicking Resume.
        self.lease.begin_resume_validation()
        if decision == "abort":
            self.lease.abort()

        self.decision = decision
        self.decided.set()
        return request

    def confirm_resume(self) -> None:
        """Called once the precondition has been re-verified — the only way back to AUTOMATION."""

        if self.lease.state is LeaseState.RESUME_VALIDATION:
            self.lease.resume()

    @property
    def controller(self) -> Controller:
        return self.lease.controller

    async def _shot(self, name: str) -> str | None:
        directory = self.evidence.dir / "handoff"
        directory.mkdir(parents=True, exist_ok=True)
        try:
            (directory / name).write_bytes(await self.surface.screenshot())
        except Exception:  # noqa: BLE001 - evidence is best effort; the lease is not
            return None
        return str(Path("handoff") / name)
