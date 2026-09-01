"""The discovery loop: observe → decide → act, with the model on top and policy underneath.

Two things are deliberate here. The model proposes; it never executes — every action passes through
`PolicyEngine.authorize()`, the *same* object replay calls, which is invariant 8. And every click is
handed to the `Recorder` before the page can change, so the run yields semantic targets rather than
the coordinates the model actually used.

The loop is bounded three ways (plan §5): a step cap, a wall clock, and loop detection on repeated
observations. Any of them ending the run is a normal outcome, not an error.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from interface_cua.discovery.model import DiscoveryModel, ModelAction, Observation
from interface_cua.discovery.recorder import RecordedTarget, Recorder
from interface_cua.observability.events import (
    ContentScanEvent,
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
from interface_cua.policy.redaction import PIIScanner, RegexPIIScanner
from interface_cua.schema.artifact import ActionSpec, InputValue, RiskLevel, RiskSpec

MAX_STEPS = 25
WALL_CLOCK_SECONDS = 180.0
#: How many identical observations in a row before we call it a loop.
LOOP_THRESHOLD = 3
#: How long to let a URL stop moving before recording where an action landed.
URL_SETTLE_SECONDS = 3.0
URL_POLL_SECONDS = 0.15


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
class RecordedOutput:
    """A value the model named, plus how to find it again on a later run."""

    name: str
    #: Kept only so the compiler can infer the type. Never written into the artifact.
    observed_value: str
    target: RecordedTarget | None
    #: The URL the value was visible on. The compiler pins extraction to the step that lands
    #: here — an output read on member detail cannot be re-read from the form page.
    captured_at_url: str
    #: The observed value matched the PII scanner. Carried into the artifact so replay masks it.
    sensitive: bool = False


@dataclass(slots=True)
class DiscoveryRun:
    run_id: str
    goal: str
    outcome: DiscoveryOutcome
    steps: list[RecordedStep] = field(default_factory=list)
    outputs: dict[str, RecordedOutput] = field(default_factory=dict)
    detail: str | None = None


#: Discovery risk is coarse by design: it exists to gate the loop, while the *artifact's* per-step
#: risk is what replay enforces. Anything that submits is treated as consequential.
_RISK_BY_ACTION = {
    "click": RiskLevel.REVERSIBLE_WRITE,
    "type": RiskLevel.REVERSIBLE_WRITE,
    "key": RiskLevel.REVERSIBLE_WRITE,
    "scroll": RiskLevel.READ,
    "screenshot": RiskLevel.READ,
    "extract": RiskLevel.READ,
}

_POLICY_ACTION = {
    "click": "click",
    "type": "fill",
    "key": "keypress",
    "scroll": "click",
    "screenshot": "extract",
    "extract": "extract",
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
        pii_scanner: PIIScanner | None = None,
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
        # Same Protocol the redactor uses, so "what counts as regulated data" has one definition
        # whether it is being masked on the way to disk or refused on the way out of a session.
        self.pii_scanner = pii_scanner or RegexPIIScanner()
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

            observation, digest, scan = await self._observe()
            turn = await self.model.propose(observation)
            if not turn.actions:
                result.outcome = DiscoveryOutcome.NO_ACTION
                result.detail = "model proposed nothing"
                return result

            # A loop is the same screen *and* the same response to it. Screen alone is too coarse:
            # focus a field, type into it, submit — three turns on a page whose text never changes,
            # which is ordinary progress, not a loop.
            recent.append(f"{digest}|{_signature(turn.actions)}")
            if _looping(recent):
                result.outcome = DiscoveryOutcome.LOOP_DETECTED
                result.detail = (
                    "the model kept answering an unchanged screen the same way; not progressing"
                )
                return result

            tool_results: list[dict[str, Any]] = []
            for action in turn.actions:
                stop = await self._handle(
                    action, index, turn.rationale_summary, result, tool_results, scan
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
        scan: ContentScanEvent,
    ) -> DiscoveryOutcome | None:
        """Execute one proposed action. Returns a terminal outcome, or None to continue."""

        if action.kind == "finish":
            return DiscoveryOutcome.FINISHED
        if action.kind == "escalate":
            return DiscoveryOutcome.ESCALATED
        # Extraction is data egress, so it is authorized like everything else — but its risk comes
        # from the *value*, not the action, so it is scanned before policy is asked. `authorize`
        # decides; this only supplies the fact.
        sensitive_extraction = (
            action.kind == "extract"
            and action.text is not None
            and bool(self.pii_scanner.scan(action.text))
        )

        decision = self.policy.authorize(
            _policy_action(action.kind),
            RiskSpec(
                level=_RISK_BY_ACTION.get(action.kind, RiskLevel.REVERSIBLE_WRITE),
                requires_confirmation=False,
            ),
            AuthorizationContext(
                self.surface.current_url, sensitive_extraction=sensitive_extraction
            ),
        )
        policy_event = PolicyDecisionEvent(
            verdict=decision.verdict.value, rule=decision.rule, origin_ok=decision.origin_ok
        )
        if decision.verdict is not PolicyVerdict.ALLOW:
            # The model asked; policy said no. It does not get to retry past this.
            self._emit(
                index, action, rationale, policy_event, None, scan, ok=False, note=decision.rule
            )
            result.detail = f"policy {decision.verdict.value}: {decision.rule}"
            return DiscoveryOutcome.POLICY_DENIED

        if action.kind == "extract":
            if action.output_name and action.text is not None:
                # Anchor the output the same way a click is anchored. The model tells us the
                # value; only the page can tell us how to find it again next run — storing the
                # value itself would bake this run's data into the capability.
                result.outputs[action.output_name] = RecordedOutput(
                    name=action.output_name,
                    observed_value=action.text,
                    target=await self._locate_text(action.text),
                    captured_at_url=self.surface.current_url,
                    sensitive=sensitive_extraction,
                )
            self._emit(
                index, action, rationale, policy_event, None, scan, ok=True, note="recorded"
            )
            tool_results.append(_ok(action, "recorded"))
            return None

        url_before = self.surface.current_url
        # Resolve semantics *before* acting — after the click the element may be gone.
        target = (
            await self.recorder.describe_point(action.x, action.y)
            if action.kind == "click" and action.x is not None and action.y is not None
            else None
        )
        if target is not None and target.snapped:
            self.evidence.notice(
                NoticeKind.CLICK_SNAPPED,
                requested=[action.x, action.y],
                actuated=list(target.point),
                control=target.accessible_name,
            )
        await self._act(action, target)
        url_after = await self._settled_url()

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
        self._emit(index, action, rationale, policy_event, target, scan, ok=True, note=url_after)
        tool_results.append(_ok(action, "done"))
        return None

    async def _settled_url(self) -> str:
        """Where the action actually landed.

        A form POST answered with a 303 has not reached its destination when the current document
        finishes loading, so reading the URL immediately records the page we came *from*. The
        compiler canonicalises routes from these values, so getting it wrong would bake the wrong
        pattern into the artifact. Polls until the URL holds still.
        """

        previous = self.surface.current_url
        deadline = time.monotonic() + URL_SETTLE_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(URL_POLL_SECONDS)
            current = self.surface.current_url
            if current == previous:
                return current
            previous = current
        return previous

    async def _locate_text(self, value: str) -> RecordedTarget | None:
        """Find where a value the model reported is actually rendered, and describe that point."""

        page = self.recorder.page
        # Exact first, then substring: the model reports the value it read ("9876.54") while the
        # page renders it decorated ("$9876.54"). Requiring an exact match would silently drop
        # most real outputs.
        for frame, exact in [(f, e) for e in (True, False) for f in [page, *page.frames]]:
            try:
                locator = frame.get_by_text(value, exact=exact).first
                if await locator.count() == 0:
                    continue
                # Short timeout: this is a best-effort anchor, not a checkpoint. If the value is
                # not stably visible we record the output without a target and the compiler skips
                # it, which is better than stalling discovery for 30s per miss.
                box = await locator.bounding_box(timeout=2_000)
            except Exception:  # noqa: BLE001, S112 - a detached/cross-origin frame is a miss
                continue
            if box:
                return await self.recorder.describe_point(
                    int(box["x"] + box["width"] / 2),
                    int(box["y"] + box["height"] / 2),
                    avoid_text=value,
                )
        return None

    async def _act(self, action: ModelAction, target: RecordedTarget | None = None) -> None:
        page = self.recorder.page
        if action.kind == "click" and action.x is not None and action.y is not None:
            # Click the control the recorder resolved, not the raw estimate. The model picks
            # *which* control; the page decides where it is. Without this, a coordinate tens of
            # pixels off actuates a container and the run stalls with nothing appearing to happen.
            x, y = target.point if target is not None else (action.x, action.y)
            await page.mouse.click(x, y)
        elif action.kind == "type" and action.text:
            await page.keyboard.type(action.text)
        elif action.kind == "key" and action.text:
            await page.keyboard.press(action.text)
        elif action.kind == "scroll":
            await page.mouse.wheel(0, int(action.raw_input.get("scroll_amount", 3)) * 100)
        await self.surface.wait_until_settled(5_000)

    async def _observe(self) -> tuple[Observation, str, ContentScanEvent]:
        screenshot = await self.surface.screenshot()
        raw = await self.surface.page_text()

        # Masked before it enters model context, not on the way to disk. The same run values that
        # are masked in the log are masked here, so a member id the model does not need to see in
        # clear text never reaches the API in the text channel.
        text = str(self.evidence.redactor.redact(raw).value)

        scan = self.scanner.scan(text)
        if scan.verdict is not ContentVerdict.CLEAN:
            # A signal, not a gate: containment comes from policy and the LLM-free replay path.
            self.evidence.notice(
                NoticeKind.CONTENT_RISK_FLAGGED,
                signals=list(scan.signals),
                scanner=scan.scanner,
            )
        observation = Observation(
            screenshot_png=screenshot, url=self.surface.current_url, page_text=text
        )
        scan_event = ContentScanEvent(
            verdict=scan.verdict.value, scanner=scan.scanner, signals=list(scan.signals)
        )
        return observation, f"{self.surface.current_url}|{hash(text)}", scan_event

    def _emit(
        self,
        index: int,
        action: ModelAction,
        rationale: str | None,
        policy: PolicyDecisionEvent,
        target: RecordedTarget | None,
        scan: ContentScanEvent,
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
                content_scan=scan,
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


#: Placeholder reference for the policy probe below. Discovery has no artifact yet, so there is no
#: real input to point at — and `authorize` only reads `action.type`.
_PROBE_VALUE = InputValue(from_input="${inputs.discovery_probe}")


def _policy_action(kind: str) -> ActionSpec:
    """A schema-valid `ActionSpec` to ask policy about, without inventing an artifact."""

    action_type = _POLICY_ACTION.get(kind, "click")
    if action_type == "fill":
        return ActionSpec(type="fill", value=_PROBE_VALUE)
    if action_type == "keypress":
        return ActionSpec(type="keypress", key="Enter")
    return ActionSpec(type=action_type)  # type: ignore[arg-type]


def _ok(action: ModelAction, message: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": action.tool_use_id, "content": message}


def _signature(actions: tuple[ModelAction, ...]) -> str:
    """What the model chose to do, ignoring identity — two clicks on the same spot match."""

    return ";".join(f"{a.kind}:{a.x},{a.y}:{a.text or ''}" for a in actions)


def _looping(recent: list[str]) -> bool:
    return len(recent) >= LOOP_THRESHOLD and len(set(recent[-LOOP_THRESHOLD:])) == 1
