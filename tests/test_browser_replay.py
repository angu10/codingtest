from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright

from interface_cua.handoff.lease import SessionLease
from interface_cua.observability.evidence import EvidenceWriter
from interface_cua.policy.engine import PolicyConfig, PolicyEngine
from interface_cua.replay.executor import ReplayExecutor
from interface_cua.schema.artifact import Capability
from interface_cua.schema.result import ValidationCheck
from interface_cua.surface.playwright_surface import PlaywrightSurface


@pytest.mark.asyncio
async def test_real_browser_replay_reaches_review_without_an_llm(demo_server: str) -> None:
    artifact = Capability.from_yaml(Path("artifacts/open-sub-account-review-v1.yaml"))
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()
        await page.goto(demo_server)
        surface = PlaywrightSurface(page, SessionLease())
        executor = ReplayExecutor(
            surface,
            PolicyEngine(PolicyConfig(allowed_origins=frozenset({demo_server}))),
            application_family="meridian-cu",
            application_version="demo-v1",
            allow_draft=True,
        )

        result = await executor.execute(
            artifact, {"member_id": "58431", "account_type": "savings"}
        )
        assert result.status == "success", result.model_dump(mode="json")
        assert result.outputs == {
            "member_name": "Morgan Chen",
            "current_balance": "9876.54",
        }
        assert await page.get_by_role("heading", name="Review Sub-Account").is_visible()
        await browser.close()


async def _replay(demo_server: str, member_id: str, evidence: EvidenceWriter | None = None):
    artifact = Capability.from_yaml(Path("artifacts/open-sub-account-review-v1.yaml"))
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.goto(demo_server)
        executor = ReplayExecutor(
            PlaywrightSurface(page, SessionLease()),
            PolicyEngine(PolicyConfig(allowed_origins=frozenset({demo_server}))),
            application_family="meridian-cu",
            application_version="demo-v1",
            allow_draft=True,
            evidence=evidence,
        )
        try:
            return await executor.execute(
                artifact, {"member_id": member_id, "account_type": "savings"}
            )
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_a_failing_run_leaves_a_complete_evidence_trail(
    demo_server: str, tmp_path: Path
) -> None:
    """The bundle is produced without anyone remembering to ask for it."""

    evidence = EvidenceWriter(tmp_path, "run-entitlement-refusal")
    result = await _replay(demo_server, "55506", evidence)
    assert result.status == "failure"
    assert result.evidence is not None and Path(result.evidence).exists()

    events = evidence.events.read()
    assert events, "no events were recorded"
    # Invariant 1 is legible in the evidence: every replay decision came from the artifact.
    assert {event["decision_source"] for event in events if "decision_source" in event} == {
        "artifact"
    }
    assert not any("rationale_summary" in event for event in events)

    # The ladder's reasoning is recorded, not just its verdict.
    ladder = [
        attempt
        for event in events
        for attempt in event.get("target", {}).get("attempts", [])
    ]
    assert any(attempt["rejected_because"].startswith("accepted") for attempt in ladder)

    # No raw member identifier reaches disk. The input was supplied as `55506`; the log may only
    # ever carry the `${inputs.member_id}` reference or a masked last-4 form.
    written = (evidence.dir / "events.jsonl").read_text(encoding="utf-8")
    assert "55506" not in written
    assert "${inputs.member_id}" in written


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("member_id", "expected_status", "expected_detail"),
    [
        ("99999", "business_outcome", "MEMBER_NOT_FOUND"),
        ("55501", "business_outcome", "MEMBER_RESTRICTED"),
        ("55503", "needs_human", "SESSION_EXPIRED"),
        ("55506", "failure", "postcondition_failed"),
    ],
)
async def test_real_browser_replay_classifies_declared_and_system_outcomes(
    demo_server: str,
    member_id: str,
    expected_status: str,
    expected_detail: str,
) -> None:
    result = await _replay(demo_server, member_id)
    assert result.status == expected_status, result.model_dump(mode="json")
    detail = getattr(result, "code", None) or getattr(result, "reason", None)
    detail = detail or getattr(result, "category", None)
    assert str(detail) == expected_detail


@pytest.mark.asyncio
async def test_replay_refuses_to_start_on_an_unrecognised_entry_screen(demo_server: str) -> None:
    """The fingerprint is checked against the live page, not taken on the caller's word."""

    artifact = Capability.from_yaml(Path("artifacts/open-sub-account-review-v1.yaml"))
    artifact.application.fingerprint.entry_landmarks = ["Commercial Lending Console"]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.goto(demo_server)
        executor = ReplayExecutor(
            PlaywrightSurface(page, SessionLease()),
            PolicyEngine(PolicyConfig(allowed_origins=frozenset({demo_server}))),
            application_family="meridian-cu",
            application_version="demo-v1",
            allow_draft=True,
        )
        result = await executor.execute(
            artifact, {"member_id": "58431", "account_type": "savings"}
        )
        await browser.close()

    assert result.status == "validation_required"
    assert result.check == ValidationCheck.ENTRY_LANDMARKS
    assert result.observed["missing"] == ["Commercial Lending Console"]


@pytest.mark.asyncio
async def test_slow_detail_load_is_absorbed_by_a_bounded_condition_wait(demo_server: str) -> None:
    """55502 stalls the detail load for 6s. The wait ends on the checkpoint, not on a timer."""

    result = await _replay(demo_server, "55502")
    assert result.status == "success", result.model_dump(mode="json")
    assert result.outputs["current_balance"] == "600.00"


@pytest.mark.asyncio
async def test_declared_interstitial_is_dismissed_and_the_run_continues(demo_server: str) -> None:
    """55504 raises a maintenance modal that hides the account table.

    The capability declares that modal, so replay dismisses it and carries on. An *undeclared*
    modal would still stop the run — recovery is scoped to what the artifact named.
    """

    result = await _replay(demo_server, "55504")
    assert result.status == "success", result.model_dump(mode="json")
    assert result.outputs["current_balance"] == "800.00"


@pytest.mark.asyncio
async def test_identical_denial_screens_are_split_by_the_contract_alone(demo_server: str) -> None:
    """The load-bearing claim: 55501 and 55506 render the same screen and classify differently.

    Nothing in the rendered page distinguishes them — same heading, same status, same styling.
    The only difference is that the capability declared one of them in ``postcondition.any_of``.
    """

    async with httpx.AsyncClient(base_url=demo_server) as client:
        restricted = (await client.get("/account-pane/55501")).text
        entitlement = (await client.get("/account-pane/55506")).text

    assert "Permission denied" in restricted and "Permission denied" in entitlement
    # No machine-readable outcome code leaks into the DOM for replay to shortcut on.
    for markup in (restricted, entitlement):
        assert "MEMBER_RESTRICTED" not in markup
        assert "AUTHORIZATION_DENIED" not in markup

    declared = await _replay(demo_server, "55501")
    undeclared = await _replay(demo_server, "55506")

    # Both stop at the same step, on the same screen. Only the contract separates them.
    assert declared.status == "business_outcome" and declared.code == "MEMBER_RESTRICTED"
    assert undeclared.status == "failure" and undeclared.retryable is False
    assert declared.step == undeclared.step == "search-member"
    # The executor never names the undeclared refusal, but hands an operator the evidence to.
    assert "servicing entitlement" in undeclared.observed
    assert "member-restricted" in undeclared.expected
