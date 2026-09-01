"""The discovery loop's bounds and its policy gate, driven by a scripted model.

`DiscoveryModel` is a protocol precisely so this file exists: every stop condition and the
policy refusal are exercised with no API key, no spend, and no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from interface_cua.discovery.model import ModelAction, ModelTurn, Observation
from interface_cua.discovery.orchestrator import DiscoveryOrchestrator, DiscoveryOutcome
from interface_cua.handoff.lease import SessionLease
from interface_cua.observability.evidence import EvidenceWriter
from interface_cua.policy.engine import PolicyConfig, PolicyEngine
from interface_cua.surface.playwright_surface import PlaywrightSurface

pytestmark = pytest.mark.asyncio


class ScriptedModel:
    """Replays a fixed list of turns. Records what it was shown, so we can assert on it."""

    def __init__(self, turns: list[ModelTurn]) -> None:
        self.turns = list(turns)
        self.seen: list[Observation] = []
        self.results: list[list[dict]] = []

    async def propose(self, observation: Observation) -> ModelTurn:
        self.seen.append(observation)
        if not self.turns:
            return ModelTurn(actions=(), rationale_summary=None, stop_reason="end_turn")
        return self.turns.pop(0)

    def record_results(self, results: list[dict]) -> None:
        self.results.append(results)


def _turn(*actions: ModelAction, why: str = "because the goal says so") -> ModelTurn:
    return ModelTurn(actions=actions, rationale_summary=why, stop_reason="tool_use")


def click(x: int, y: int, tid: str = "t1") -> ModelAction:
    return ModelAction(kind="click", tool_use_id=tid, x=x, y=y)


async def _run(demo_server, tmp_path, model, *, origins=None, max_steps=25, path="/"):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.goto(f"{demo_server}{path}")
        surface = PlaywrightSurface(page, SessionLease())
        evidence = EvidenceWriter(tmp_path, "disc-test")
        orchestrator = DiscoveryOrchestrator(
            surface=surface,
            page=page,
            model=model,
            policy=PolicyEngine(
                PolicyConfig(allowed_origins=frozenset(origins or {demo_server}))
            ),
            evidence=evidence,
            goal="reach the sub-account review screen",
            max_steps=max_steps,
        )
        try:
            return await orchestrator.run(), evidence
        finally:
            await browser.close()


async def test_finish_ends_the_run_and_keeps_extracted_outputs(demo_server, tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            _turn(
                ModelAction(
                    kind="extract",
                    tool_use_id="e1",
                    output_name="member_name",
                    text="Morgan Chen",
                )
            ),
            _turn(ModelAction(kind="finish", tool_use_id="f1", reason="review screen reached")),
        ]
    )
    run, _ = await _run(demo_server, tmp_path, model)
    assert run.outcome is DiscoveryOutcome.FINISHED
    assert run.detail == "review screen reached"
    assert set(run.outputs) == {"member_name"}
    assert run.outputs["member_name"].observed_value == "Morgan Chen"


async def test_escalate_is_a_normal_outcome_not_an_error(demo_server, tmp_path: Path) -> None:
    model = ScriptedModel(
        [_turn(ModelAction(kind="escalate", tool_use_id="x1", reason="screen is unfamiliar"))]
    )
    run, _ = await _run(demo_server, tmp_path, model)
    assert run.outcome is DiscoveryOutcome.ESCALATED
    assert run.detail == "screen is unfamiliar"


async def test_step_cap_bounds_a_model_that_never_finishes(demo_server, tmp_path: Path) -> None:
    """A model that keeps going forever is stopped by the cap.

    The cap is set below LOOP_THRESHOLD so the loop detector cannot fire first — otherwise this
    would pass for the wrong reason and tell us nothing about the cap.
    """

    model = ScriptedModel(
        [_turn(ModelAction(kind="scroll", tool_use_id=f"s{i}")) for i in range(50)]
    )
    run, _ = await _run(demo_server, tmp_path, model, max_steps=2)
    assert run.outcome is DiscoveryOutcome.STEP_LIMIT
    assert len(run.steps) == 2
    assert model.turns, "the model still had turns left — the loop stopped it, as intended"


async def test_repeated_identical_screens_are_detected_as_a_loop(
    demo_server, tmp_path: Path
) -> None:
    """Scrolling a page that cannot scroll leaves the screen unchanged — that is the loop signal."""

    model = ScriptedModel(
        [_turn(ModelAction(kind="scroll", tool_use_id=f"s{i}")) for i in range(20)]
    )
    run, _ = await _run(demo_server, tmp_path, model, max_steps=20)
    assert run.outcome is DiscoveryOutcome.LOOP_DETECTED
    assert "not progressing" in (run.detail or "")


async def test_policy_denies_an_action_on_a_disallowed_origin(demo_server, tmp_path: Path) -> None:
    """Policy sits below the model: proposing an action is not permission to take it."""

    model = ScriptedModel([_turn(click(100, 100))])
    run, _ = await _run(
        demo_server, tmp_path, model, origins={"https://somewhere.else.example"}
    )
    assert run.outcome is DiscoveryOutcome.POLICY_DENIED
    assert "origin:not_allowed" in (run.detail or "")
    assert run.steps == []  # denied before anything touched the page


async def test_a_click_is_recorded_semantically_not_as_coordinates(
    demo_server, tmp_path: Path
) -> None:
    """The whole point: the model clicks a pixel, the run stores what was under it."""

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.goto(demo_server)
        box = await page.locator("button.btn").bounding_box()
        await browser.close()
    assert box is not None
    point = (int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))

    model = ScriptedModel(
        [
            _turn(click(*point), why="clicking Search to look the member up"),
            _turn(ModelAction(kind="finish", tool_use_id="f1", reason="done")),
        ]
    )
    run, evidence = await _run(demo_server, tmp_path, model)

    assert run.outcome is DiscoveryOutcome.FINISHED
    step = run.steps[0]
    assert step.target is not None
    assert step.target.role == "button"
    assert step.target.accessible_name == "Search"

    # Discovery events carry the model's reasoning; replay events are forbidden from doing so.
    events = evidence.events.read()
    model_events = [e for e in events if e.get("decision_source") == "model"]
    assert model_events, events
    assert any("Search" in (e.get("rationale_summary") or "") for e in model_events)
