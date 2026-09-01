"""The consequential write, and what happens when its answer is ambiguous.

Member 55505's create *succeeds server-side* and then returns a 500. That is the case retries were
invented for and the case retries must not be used for: the record exists, and clicking Create
again would make a second one. Invariant 4 in a single scenario.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright

from demo_app.data import reset_members
from interface_cua.handoff.lease import SessionLease
from interface_cua.observability.evidence import EvidenceWriter
from interface_cua.policy.engine import PolicyConfig, PolicyEngine
from interface_cua.replay.executor import ReplayExecutor
from interface_cua.schema.artifact import Capability
from interface_cua.surface.playwright_surface import PlaywrightSurface

pytestmark = pytest.mark.asyncio

ARTIFACT = Path("artifacts/create-sub-account-v1.yaml")
CONFIRMED = frozenset({"submit-create"})


async def _run(
    demo_server: str,
    tmp_path: Path,
    member: str,
    nickname: str,
    deposit: str = "250.00",
    **kwargs,
):
    artifact = Capability.from_yaml(ARTIFACT)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.goto(demo_server)
        evidence = EvidenceWriter(tmp_path, f"create-{member}", sensitive_values=frozenset({member}))
        executor = ReplayExecutor(
            PlaywrightSurface(page, SessionLease()),
            PolicyEngine(PolicyConfig(allowed_origins=frozenset({demo_server}))),
            application_family="meridian-cu",
            application_version="demo-v1",
            allow_draft=True,
            evidence=evidence,
        )
        try:
            result = await executor.execute(
                artifact,
                {
                    "member_id": member,
                    "account_type": "savings",
                    "nickname": nickname,
                    "opening_deposit": deposit,
                },
                **kwargs,
            )
            return result, evidence
        finally:
            await browser.close()


def _sub_accounts(demo_server: str, member: str) -> list[dict]:
    """Ask the app directly — independent of anything replay believes."""

    response = httpx.get(f"{demo_server}/api/members/{member}/sub-accounts", timeout=5)
    return response.json()["sub_accounts"]


async def test_a_consequential_write_requires_confirmation_before_it_runs(
    demo_server: str, tmp_path: Path
) -> None:
    """Not confirmed means not attempted. The gate is policy, ahead of the click."""

    reset_members()
    result, _ = await _run(demo_server, tmp_path, "12345", "Holiday Fund")
    assert result.status == "needs_human"
    assert result.reason == "risk:consequential_write"
    assert result.step == "submit-create"
    # Nothing was submitted, so nothing exists.
    assert _sub_accounts(demo_server, "12345") == []


async def test_a_confirmed_write_completes(demo_server: str, tmp_path: Path) -> None:
    reset_members()
    result, _ = await _run(
        demo_server, tmp_path, "12345", "Holiday Fund", confirmed_steps=CONFIRMED
    )
    assert result.status == "success", result.model_dump(mode="json")
    assert result.reconciled is False  # a clean run, not a recovered one
    created = _sub_accounts(demo_server, "12345")
    assert len(created) == 1
    assert created[0]["nickname"] == "Holiday Fund"


async def test_a_rejected_deposit_is_a_declared_business_outcome(
    demo_server: str, tmp_path: Path
) -> None:
    """A field-level validation error is an answer, not a crash.

    The amount is a valid decimal, so it passes input validation and reaches the browser. The
    *application* is the thing that refuses it, and because the capability declared that refusal
    the caller gets a code instead of a stack trace. Nothing is created, and the run stops before
    the consequential step even though it was confirmed.
    """

    reset_members()
    # Compared before and after rather than against []: `reset_members` runs in this process, but
    # the app under test is a subprocess, so it cannot clear the server's state. What matters here
    # is that *this run* created nothing.
    before = _sub_accounts(demo_server, "12345")
    result, _ = await _run(
        demo_server, tmp_path, "12345", "Holiday Fund",
        deposit="-5.00", confirmed_steps=CONFIRMED,
    )

    assert result.status == "business_outcome", result.model_dump(mode="json")
    assert result.code == "DEPOSIT_INVALID"
    assert result.step == "continue-to-review"
    assert _sub_accounts(demo_server, "12345") == before


async def test_an_unparseable_deposit_never_reaches_the_browser(
    demo_server: str, tmp_path: Path
) -> None:
    """Type validation is a different layer from business validation, and it runs first."""

    reset_members()
    result, _ = await _run(demo_server, tmp_path, "12345", "Holiday Fund", deposit="abc")

    assert result.status == "failure"
    assert result.category.value == "precondition_failed"
    assert result.step == "input-validation"


async def test_an_ambiguous_write_is_reconciled_and_never_re_clicked(
    demo_server: str, tmp_path: Path
) -> None:
    """55505's write lands, then the response 500s. The record must not be created twice."""

    reset_members()
    result, evidence = await _run(
        demo_server, tmp_path, "55505", "Rainy Day", confirmed_steps=CONFIRMED
    )

    # Resolved as success, but flagged — a caller can tell this apart from a clean run.
    assert result.status == "success", result.model_dump(mode="json")
    assert result.reconciled is True

    # Necessary but not sufficient: the demo app dedupes identical payloads, so this would still
    # read 1 if replay had re-clicked. The STEP_RETRIED assertion below is the one that bites.
    created = _sub_accounts(demo_server, "55505")
    assert len(created) == 1, f"the write was repeated: {created}"
    assert created[0]["nickname"] == "Rainy Day"

    kinds = [event.get("kind") for event in evidence.events.read() if "kind" in event]
    assert "RECONCILIATION_STARTED" in kinds
    assert "RECONCILIATION_CONFIRMED" in kinds
    assert "STEP_RETRIED" not in kinds, "a consequential write must never be retried"


async def test_reconciliation_reports_absence_as_a_retryable_failure(
    demo_server: str, tmp_path: Path
) -> None:
    """If the probe finds nothing, the write did not land — so a fresh run is safe.

    Simulated by looking for a nickname the app was never given: the submit is ambiguous for
    55505 either way, but the probe cannot find this record because it does not exist.
    """

    reset_members()
    artifact = Capability.from_yaml(ARTIFACT)
    # Point the probe at a value that will never appear, leaving the flow otherwise identical.
    artifact.reconciliation.landed.from_input = None
    artifact.reconciliation.landed.value = "a nickname nothing will ever contain"

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
            artifact,
            {
                "member_id": "55505",
                "account_type": "savings",
                "nickname": "Absent",
                "opening_deposit": "250.00",
            },
            confirmed_steps=CONFIRMED,
        )
        await browser.close()

    assert result.status == "failure"
    assert result.category.value == "application_error"
    # Retryable, because nothing landed — the opposite of retrying the click that was ambiguous.
    assert result.retryable is True
    assert "did not land" in result.observed


async def test_the_probe_is_authorized_like_any_other_navigation(
    demo_server: str, tmp_path: Path
) -> None:
    """A probe that could leave the allowlist would be a hole, not a safety net."""

    reset_members()
    artifact = Capability.from_yaml(ARTIFACT)
    artifact.reconciliation.probe_route = "https://exfiltrate.example/sub-accounts"

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
            artifact,
            {
                "member_id": "55505",
                "account_type": "savings",
                "nickname": "Blocked",
                "opening_deposit": "250.00",
            },
            confirmed_steps=CONFIRMED,
        )
        await browser.close()

    assert result.status == "needs_human"
    assert result.reason == "RECONCILIATION_INCONCLUSIVE"
