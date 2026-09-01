"""The discovery loop: observe → decide → act, with the model on top and policy underneath.

Two things are deliberate here. The model proposes; it never executes — every action passes through
`PolicyEngine.authorize()`, the *same* object replay calls, which is invariant 8. And every click is
handed to the `Recorder` before the page can change, so the run yields semantic targets rather than
the coordinates the model actually used.

The loop is bounded three ways (plan §5): a step cap, a wall clock, and loop detection on repeated
observations. Any of them ending the run is a normal outcome, not an error.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from interface_cua.discovery.model import DiscoveryModel, ModelAction, Observation
from interface_cua.discovery.recorder import RecordedTarget, Recorder
from interface_cua.observability.events import (
    NoticeKind,
    PolicyDecisionEvent,
    ProposedAction,
    RunEvent,
    StepResult,
    TargetEvent,
)
from interface_cua.observability.events import (
    Observation as ObservationEvent,
)
from interface_cua.observability.evidence import EvidenceWriter
from interface_cua.policy.content_scanner import (
    ContentRiskScanner,
    ContentVerdict,
    HeuristicContentRiskScanner,
)
from interface_cua.policy.engine import AuthorizationContext, PolicyEngine, PolicyVerdict
from interface_cua.schema.artifact import ActionSpec, RiskLevel, RiskSpec

MAX_STEPS = 25
WALL_CLOCK_SECONDS = 180.0
#: How many identical observations in a row before we call it a loop.
LOOP_THRESHOLD = 3


class DiscoveryOutcome(StrEnum):
    FINISHED = "finished"
    ESCALATED = "escalated"
    STEP_LIMIT = "step_limit"
    TIME_LIMIT = "time_limit"
    LOOP_DETECTED = "loop_detected"
    POLICY_DENIED = "policy_denied"
    NO_ACTION = "no_action"


@dataclass(slots=True)
class RecordedStep:
    """One authorized, executed action and the semantics recovered for it."""

    index: int
    action: ModelAction
    url_before: str
    url_after: str
    target: RecordedTarget | None
    rationale: str | None


@dataclass(slots=True)
class DiscoveryRun:
    run_id: str
    goal: str
    outcome: DiscoveryOutcome
    steps: list[RecordedStep] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    detail: str | None = None


#: Discovery risk is coarse by design: it exists to gate the loop, while the *artifact's* per-step
#: risk is what replay enforces. Anything that submits is treated as consequential.
_RISK_BY_ACTION = {
    "click": RiskLevel.REVERSIBLE_WRITE,
    "type": RiskLevel.REVERSIBLE_WRITE,
    "key": RiskLevel.REVERSIBLE_WRITE,
    "scroll": RiskLevel.READ,
    "screenshot": RiskLevel.READ,
}

_POLICY_ACTION = {
    "click": "click",
    "type": "fill",
    "key": "keypress",
    "scroll": "click",
    "screenshot": "extract",
}


class DiscoveryOrchestrator:
    def __init__(
        self,
        *,
        surface: Any,
        page: Any,
        model: DiscoveryModel,
        policy: PolicyEngine,
        evidence: EvidenceWriter,
        goal: str,
        scanner: ContentRiskScanner | None = None,
        max_steps: int = MAX_STEPS,
        wall_clock_seconds: float = WALL_CLOCK_SECONDS,
    ) -> None:
        self.surface = surface
        self.recorder = Recorder(page)
        self.model = model
        self.policy = policy
        self.evidence = evidence
        self.goal = goal
        self.scanner = scanner or HeuristicContentRiskScanner()
        self.max_steps = max_steps
        self.wall_clock_seconds = wall_clock_seconds

    async def run(self) -> DiscoveryRun:
        result = DiscoveryRun(run_id=self.evidence.run_id, goal=self.goal, outcome=DiscoveryOutcome.STEP_LIMIT)
        deadline = time.monotonic() + self.wall_clock_seconds
        recent: list[str] = []

        for index in range(self.max_steps):
            if time.monotonic() >= deadline:
                result.outcome = DiscoveryOutcome.TIME_LIMIT
                return result

            observation, digest = await self._observe()
            recent.append(digest)
            if _looping(recent):
                result.outcome = DiscoveryOutcome.LOOP_DETECTED
                result.detail = "the same screen came back unchanged; the model is not progressing"
                return result

            turn = await self.model.propose(observation)
            if not turn.actions:
                result.outcome = DiscoveryOutcome.NO_ACTION
                result.detail = "model proposed nothing"
                return result

            tool_results: list[dict[str, Any]] = []
            for action in turn.actions:
                stop = await self._handle(
                    action, index, turn.rationale_summary, result, tool_results
                )
                if stop is not None:
                    result.outcome = stop
                    if action.reason:
                        result.detail = action.reason
                    self.model.record_results(tool_results)
                    return result
            self.model.record_results(tool_results)

        return result

    async def _handle(
        self,
        action: ModelAction,
        index: int,
        rationale: str | None,
        result: DiscoveryRun,
        tool_results: list[dict[str, Any]],
    ) -> DiscoveryOutcome | None:
        """Execute one proposed action. Returns a terminal outcome, or None to continue."""

        if action.kind == "finish":
            return DiscoveryOutcome.FINISHED
        if action.kind == "escalate":
            return DiscoveryOutcome.ESCALATED
        if action.kind == "extract":
            if action.output_name and action.text is not None:
                result.outputs[action.output_name] = action.text
            tool_results.append(_ok(action, "recorded"))
            return None

        decision = self.policy.authorize(
            ActionSpec(type=_POLICY_ACTION.get(action.kind, "click")),  # type: ignore[arg-type]
            RiskSpec(
                level=_RISK_BY_ACTION.get(action.kind, RiskLevel.REVERSIBLE_WRITE),
                requires_confirmation=False,
            ),
            AuthorizationContext(self.surface.current_url),
        )
        policy_event = PolicyDecisionEvent(
            verdict=decision.verdict.value, rule=decision.rule, origin_ok=decision.origin_ok
        )
        if decision.verdict is not PolicyVerdict.ALLOW:
            # The model asked; policy said no. It does not get to retry past this.
            self._emit(index, action, rationale, policy_event, None, ok=False, note=decision.rule)
            result.detail = f"policy {decision.verdict.value}: {decision.rule}"
            return DiscoveryOutcome.POLICY_DENIED

        url_before = self.surface.current_url
        # Resolve semantics *before* acting — after the click the element may be gone.
        target = (
            await self.recorder.describe_point(action.x, action.y)
            if action.kind == "click" and action.x is not None and action.y is not None
            else None
        )
        await self._act(action)
        url_after = self.surface.current_url

        result.steps.append(
            RecordedStep(
                index=index,
                action=action,
                url_before=url_before,
                url_after=url_after,
                target=target,
                rationale=rationale,
            )
        )
        self._emit(index, action, rationale, policy_event, target, ok=True, note=url_after)
        tool_results.append(_ok(action, "done"))
        return None

    async def _act(self, action: ModelAction) -> None:
        page = self.recorder.page
        if action.kind == "click" and action.x is not None and action.y is not None:
            await page.mouse.click(action.x, action.y)
        elif action.kind == "type" and action.text:
            await page.keyboard.type(action.text)
        elif action.kind == "key" and action.text:
            await page.keyboard.press(action.text)
        elif action.kind == "scroll":
            await page.mouse.wheel(0, int(action.raw_input.get("scroll_amount", 3)) * 100)
        await self.surface.wait_until_settled(5_000)

    async def _observe(self) -> tuple[Observation, str]:
        screenshot = await self.surface.screenshot()
        text = await self.surface.page_text()
        scan = self.scanner.scan(text)
        if scan.verdict is not ContentVerdict.CLEAN:
            # Logged before the text reaches model context — that is the point of the scan. It is
            # a signal, not a gate: containment comes from policy and the LLM-free replay path.
            self.evidence.notice(
                NoticeKind.CONTENT_RISK_FLAGGED,
                signals=list(scan.signals),
                scanner=scan.scanner,
            )
        observation = Observation(
            screenshot_png=screenshot, url=self.surface.current_url, page_text=text
        )
        return observation, f"{self.surface.current_url}|{hash(text)}"

    def _emit(
        self,
        index: int,
        action: ModelAction,
        rationale: str | None,
        policy: PolicyDecisionEvent,
        target: RecordedTarget | None,
        *,
        ok: bool,
        note: str,
    ) -> None:
        self.evidence.emit(
            RunEvent(
                run_id=self.evidence.run_id,
                step_index=index,
                decision_source="model",
                observation=ObservationEvent(url=self.surface.current_url),
                proposed_action=ProposedAction(type=action.kind, x=action.x, y=action.y),
                policy_decision=policy,
                target=(
                    None
                    if target is None
                    else _target_event(target)
                ),
                rationale_summary=rationale,
                result=StepResult(ok=ok, elapsed_ms=0, postcondition=note),
            )
        )


def _target_event(target: RecordedTarget) -> TargetEvent:
    return TargetEvent(
        frame=target.frame,
        strategy=target.target.strategies[0].model_dump(mode="json"),
        unique=True,
        attempts=[s.model_dump(mode="json") for s in target.target.strategies],
    )


def _ok(action: ModelAction, message: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": action.tool_use_id, "content": message}


def _looping(recent: list[str]) -> bool:
    return len(recent) >= LOOP_THRESHOLD and len(set(recent[-LOOP_THRESHOLD:])) == 1
