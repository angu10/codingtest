"""Discovery → compile → replay, closing the loop with no model in either half.

The run is driven by a scripted `DiscoveryModel` and the compiled artifact is then executed by the
LLM-free executor. If this passes, the two halves of the thesis genuinely fit together.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from interface_cua.discovery.compiler import CompilationError, compile_run
from interface_cua.discovery.model import ModelAction, ModelTurn, Observation
from interface_cua.discovery.orchestrator import DiscoveryOrchestrator, DiscoveryOutcome
from interface_cua.handoff.lease import SessionLease
from interface_cua.observability.evidence import EvidenceWriter
from interface_cua.policy.engine import PolicyConfig, PolicyEngine
from interface_cua.replay.executor import ReplayExecutor
from interface_cua.surface.playwright_surface import PlaywrightSurface

pytestmark = pytest.mark.asyncio

MEMBER = "58431"


class Scripted:
    """Turns are produced lazily so each one can look at the *current* screen."""

    def __init__(self, plan) -> None:
        self.plan = plan
        self.step = 0

    async def propose(self, observation: Observation) -> ModelTurn:
        turn = await self.plan(self.step, observation)
        self.step += 1
        return turn

    def record_results(self, results) -> None:
        return None


async def _centre(scope, selector: str) -> tuple[int, int]:
    box = await scope.locator(selector).first.bounding_box()
    assert box is not None, selector
    return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)


async def _discover(demo_server: str, tmp_path: Path):
    """Walk Member Search → Detail → Sub-Account form, the way the model would."""

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.goto(demo_server)
        surface = PlaywrightSurface(page, SessionLease())

        async def plan(step: int, _observation: Observation) -> ModelTurn:
            if step == 0:
                x, y = await _centre(page, 'input[name="member_id"]')
                return ModelTurn(
                    actions=(ModelAction(kind="click", tool_use_id="c0", x=x, y=y),),
                    rationale_summary="focus the member id field",
                    stop_reason="tool_use",
                )
            if step == 1:
                return ModelTurn(
                    actions=(ModelAction(kind="type", tool_use_id="t1", text=MEMBER),),
                    rationale_summary="enter the member reference",
                    stop_reason="tool_use",
                )
            if step == 2:
                x, y = await _centre(page, "button.btn")
                return ModelTurn(
                    actions=(ModelAction(kind="click", tool_use_id="c2", x=x, y=y),),
                    rationale_summary="search for the member",
                    stop_reason="tool_use",
                )
            if step == 3:
                frame = page.frame(name="account-frame")
                assert frame is not None
                x, y = await _centre(frame, "a.btn")
                return ModelTurn(
                    actions=(
                        ModelAction(
                            kind="extract",
                            tool_use_id="e3",
                            output_name="current_balance",
                            text="9876.54",
                        ),
                        ModelAction(kind="click", tool_use_id="c3", x=x, y=y),
                    ),
                    rationale_summary="record the savings balance, then open the sub-account form",
                    stop_reason="tool_use",
                )
            return ModelTurn(
                actions=(ModelAction(kind="finish", tool_use_id="f", reason="form reached"),),
                rationale_summary="the sub-account form is open",
                stop_reason="tool_use",
            )

        orchestrator = DiscoveryOrchestrator(
            surface=surface,
            page=page,
            model=Scripted(plan),
            policy=PolicyEngine(PolicyConfig(allowed_origins=frozenset({demo_server}))),
            evidence=EvidenceWriter(tmp_path, "disc-compile"),
            goal="open the savings sub-account form",
        )
        try:
            return await orchestrator.run()
        finally:
            await browser.close()


async def test_compiler_parameterises_the_route_it_discovered(
    demo_server: str, tmp_path: Path
) -> None:
    """The recorder saw /member/58431; the artifact must say /member/:member_id."""

    run = await _discover(demo_server, tmp_path)
    assert run.outcome is DiscoveryOutcome.FINISHED, run.detail

    artifact = compile_run(
        run,
        capability_id="discovered-open-sub-account",
        description="Reach the sub-account form, discovered end to end.",
        inputs={"member_id": MEMBER},
        application_family="meridian-cu",
        application_version="demo-v1",
        entry_landmarks=["Member Search"],
        model_id="scripted-test-model",
        operator="pytest",
    )

    serialised = artifact.model_dump_json()
    assert MEMBER not in serialised, "the discovered member id leaked into the artifact"
    assert "${inputs.member_id}" in serialised

    patterns = [s.precondition.pattern for s in artifact.steps]
    assert any(":member_id" in p for p in patterns), patterns
    assert artifact.approval_state.value == "draft"
    assert artifact.provenance.discovery_run_id == "disc-compile"


async def test_compiled_artifact_stores_semantics_never_coordinates(
    demo_server: str, tmp_path: Path
) -> None:
    run = await _discover(demo_server, tmp_path)
    artifact = compile_run(
        run,
        capability_id="discovered-open-sub-account",
        description="Reach the sub-account form.",
        inputs={"member_id": MEMBER},
        application_family="meridian-cu",
        application_version="demo-v1",
        entry_landmarks=["Member Search"],
        model_id="scripted-test-model",
        operator="pytest",
    )
    clicks = [s for s in artifact.steps if s.action.type == "click"]
    assert clicks, "expected at least one click step"
    for step in clicks:
        assert step.target is not None
        assert step.target.strategies
    assert '"x"' not in artifact.model_dump_json()

    # The output was anchored to where it was rendered, not to the value that was read.
    balance = next((o for o in artifact.outputs if o.name == "current_balance"), None)
    assert balance is not None, f"output was not anchored; got {[o.name for o in artifact.outputs]}"
    assert balance.type.value == "decimal"
    assert "9876.54" not in balance.extraction.model_dump_json(), (
        "the discovered value leaked into the extraction target"
    )


async def test_compiled_artifact_actually_replays(demo_server: str, tmp_path: Path) -> None:
    """The point of the whole exercise: what discovery produced, replay can execute."""

    run = await _discover(demo_server, tmp_path)
    artifact = compile_run(
        run,
        capability_id="discovered-open-sub-account",
        description="Reach the sub-account form.",
        inputs={"member_id": MEMBER},
        application_family="meridian-cu",
        application_version="demo-v1",
        entry_landmarks=["Member Search"],
        model_id="scripted-test-model",
        operator="pytest",
    )

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
        result = await executor.execute(artifact, {"member_id": MEMBER})
        await browser.close()

    assert result.status == "success", result.model_dump(mode="json")


async def test_compiler_refuses_to_hard_code_a_value_that_is_not_an_input(
    demo_server: str, tmp_path: Path
) -> None:
    """A typed literal with no matching input would bake this run's data into the capability."""

    run = await _discover(demo_server, tmp_path)
    with pytest.raises(CompilationError, match="not a declared input"):
        compile_run(
            run,
            capability_id="discovered-open-sub-account",
            description="Reach the sub-account form.",
            inputs={"unrelated": "zzz"},
            application_family="meridian-cu",
            application_version="demo-v1",
            entry_landmarks=["Member Search"],
            model_id="scripted-test-model",
            operator="pytest",
        )
