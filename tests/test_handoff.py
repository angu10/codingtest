"""Escalation and handoff, driven against a real browser.

Member 55503 hits a session-expired pane the capability declares as an escalation. The run stops,
a human takes over the *same* page, does what only they can, and hands control back — and the
resumed step re-verifies its own precondition rather than trusting the human left things tidy.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright

from interface_cua.handoff.console import build_console
from interface_cua.handoff.intervention import HandoffCoordinator
from interface_cua.handoff.lease import Controller, LeaseState, LeaseViolation, SessionLease
from interface_cua.observability.evidence import EvidenceWriter
from interface_cua.policy.engine import PolicyConfig, PolicyEngine
from interface_cua.replay.executor import ReplayExecutor
from interface_cua.schema.artifact import Capability
from interface_cua.surface.playwright_surface import PlaywrightSurface

ARTIFACT = Path("artifacts/open-sub-account-review-v1.yaml")


@dataclass(slots=True)
class Harness:
    playwright: Any
    browser: Any
    page: Any
    lease: SessionLease
    surface: PlaywrightSurface
    coordinator: HandoffCoordinator
    executor: ReplayExecutor
    evidence: EvidenceWriter

    async def close(self) -> None:
        await self.browser.close()
        await self.playwright.stop()


async def _session(demo_server: str, tmp_path: Path, label: str) -> Harness:
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    page = await browser.new_page(viewport={"width": 1280, "height": 800})
    await page.goto(demo_server)

    lease = SessionLease()
    surface = PlaywrightSurface(page, lease)
    evidence = EvidenceWriter(tmp_path, f"handoff-{label}")
    coordinator = HandoffCoordinator(
        lease=lease,
        surface=surface,
        page=page,
        evidence=evidence,
        sensitive_fields=frozenset({"member_id"}),
    )
    executor = ReplayExecutor(
        surface,
        PolicyEngine(PolicyConfig(allowed_origins=frozenset({demo_server}))),
        application_family="meridian-cu",
        application_version="demo-v1",
        allow_draft=True,
        evidence=evidence,
    )
    return Harness(playwright, browser, page, lease, surface, coordinator, executor, evidence)


@pytest.mark.asyncio
async def test_escalation_hands_over_the_lease_and_records_the_human(
    demo_server: str, tmp_path: Path
) -> None:
    harness = await _session(demo_server, tmp_path, "55503")
    artifact = Capability.from_yaml(ARTIFACT)
    try:
        result = await harness.executor.execute(
            artifact, {"member_id": "55503", "account_type": "savings"}
        )
        assert result.status == "needs_human"
        assert result.reason == "SESSION_EXPIRED"

        request = await harness.coordinator.raise_request(
            run_id=harness.evidence.run_id,
            capability_id=artifact.capability.id,
            step_id=result.step,
            reason="the servicing pane reported an expired session",
        )
        # Automation must be genuinely stopped before the human touches anything.
        assert harness.lease.state is LeaseState.HUMAN_CONTROL
        assert harness.coordinator.controller is Controller.HUMAN
        assert request.before_screenshot is not None

        # The human does what only they can, in the same window.
        await harness.page.goto(f"{demo_server}/")
        await harness.page.get_by_role("textbox", name="Member ID").fill("58431")
        await harness.page.get_by_role("button", name="Search").click()
        await harness.page.wait_for_load_state("domcontentloaded")

        resolved = await harness.coordinator.resolve("resume")
        assert resolved.resolution == "resume"
        assert resolved.after_screenshot is not None
        assert resolved.human_actions, "nothing captured while the human held the lease"

        described = " | ".join(action.describe() for action in resolved.human_actions)
        assert "Member ID" in described or "Search" in described, described
        # Masked inside the page, so the raw value never reached this process at all.
        assert "58431" not in described, described
        assert "***8431" in described, described
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_automation_cannot_act_while_the_human_holds_the_lease(
    demo_server: str, tmp_path: Path
) -> None:
    """Invariant 5 is enforced at the surface, not by convention."""

    harness = await _session(demo_server, tmp_path, "lease")
    try:
        await harness.coordinator.raise_request(
            run_id="r", capability_id="c", step_id="s", reason="testing the lease"
        )
        assert harness.coordinator.controller is Controller.HUMAN

        # Exactly what a racing automation thread would do against the shared surface.
        with pytest.raises(LeaseViolation, match="AUTOMATION cannot mutate"):
            await harness.surface.navigate(f"{demo_server}/")
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_resume_revalidates_instead_of_trusting_the_step_number(
    demo_server: str, tmp_path: Path
) -> None:
    """A human who wanders off is caught by the precondition, not by bookkeeping."""

    harness = await _session(demo_server, tmp_path, "wander")
    artifact = Capability.from_yaml(ARTIFACT)
    try:
        await harness.coordinator.raise_request(
            run_id="r",
            capability_id=artifact.capability.id,
            step_id="search-member",
            reason="testing resume validation",
        )
        # The human leaves the session somewhere the capability cannot continue from.
        await harness.page.goto(f"{demo_server}/service/x4m9p/58431/sav-42")
        await harness.coordinator.resolve("resume")
        harness.coordinator.confirm_resume()
        assert harness.lease.state is LeaseState.AUTOMATION_ACTIVE

        result = await harness.executor.execute(
            artifact,
            {"member_id": "58431", "account_type": "savings"},
            resume_from="search-member",
        )
        assert result.status == "failure"
        assert result.category.value == "precondition_failed"
        assert result.step == "search-member"
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_resume_continues_the_run_when_the_session_is_left_usable(
    demo_server: str, tmp_path: Path
) -> None:
    """The other side of the same gate: a human who tidied up is allowed through."""

    harness = await _session(demo_server, tmp_path, "tidy")
    artifact = Capability.from_yaml(ARTIFACT)
    try:
        await harness.coordinator.raise_request(
            run_id="r",
            capability_id=artifact.capability.id,
            step_id="fill-member-id",
            reason="testing a clean resume",
        )
        await harness.page.goto(f"{demo_server}/")
        await harness.coordinator.resolve("resume")
        harness.coordinator.confirm_resume()

        result = await harness.executor.execute(
            artifact,
            {"member_id": "58431", "account_type": "savings"},
            resume_from="fill-member-id",
        )
        assert result.status == "success", result.model_dump(mode="json")
        assert result.outputs["member_name"] == "Morgan Chen"
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_abort_is_terminal(demo_server: str, tmp_path: Path) -> None:
    harness = await _session(demo_server, tmp_path, "abort")
    try:
        await harness.coordinator.raise_request(
            run_id="r", capability_id="c", step_id="s", reason="testing abort"
        )
        await harness.coordinator.resolve("abort")
        assert harness.lease.state is LeaseState.ABORT
        with pytest.raises(LeaseViolation, match="not allowed while lease is ABORT"):
            await harness.surface.navigate(f"{demo_server}/")
    finally:
        await harness.close()


class _FakePage:
    """Enough page for the coordinator; the console never needs a real browser."""

    async def expose_binding(self, name: str, handler: Any) -> None: ...

    async def add_init_script(self, script: str) -> None: ...

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        return None


class _FakeSurface:
    current_url = "http://127.0.0.1:8000/member/55503"

    async def screenshot(self) -> bytes:
        return b"\x89PNG\r\n\x1a\n"


def test_console_shows_the_request_and_takes_a_decision(tmp_path: Path) -> None:
    """Deliberately synchronous.

    `TestClient` drives its own event loop, so a coordinator wired to a real Playwright page would
    deadlock the moment a route awaited a call bound to the outer loop. The console is pure HTTP —
    testing it against a real browser would prove nothing and cost a hang.
    """

    coordinator = HandoffCoordinator(
        lease=SessionLease(),
        surface=_FakeSurface(),
        page=_FakePage(),
        evidence=EvidenceWriter(tmp_path, "console"),
    )
    client = TestClient(build_console(coordinator))
    assert "No intervention pending" in client.get("/").text

    # A run raises the request, not the UI — so drive that part directly.
    asyncio.run(
        coordinator.raise_request(
            run_id="run-7",
            capability_id="open-sub-account-review",
            step_id="search-member",
            reason="the servicing pane reported an expired session",
        )
    )

    rendered = client.get("/").text
    assert "Automation is paused" in rendered
    assert "expired session" in rendered
    assert "search-member" in rendered

    payload = client.get("/intervention").json()
    assert payload["step_id"] == "search-member"
    assert payload["resolution"] is None

    assert "Decision recorded: resume" in client.post("/resume").text
    assert coordinator.decision == "resume"
