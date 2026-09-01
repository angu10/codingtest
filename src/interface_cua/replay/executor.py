"""LLM-free replay executor whose decision graph is entirely artifact-driven."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin

from interface_cua.handoff.lease import LeaseViolation
from interface_cua.observability.events import (
    NoticeKind,
    PolicyDecisionEvent,
    ProposedAction,
    RunEvent,
    StepResult,
    TargetEvent,
)
from interface_cua.observability.evidence import EvidenceWriter
from interface_cua.policy.engine import (
    AuthorizationContext,
    PolicyEngine,
    PolicyVerdict,
)
from interface_cua.replay.polling import poll_until
from interface_cua.replay.reconcile import Reconciler
from interface_cua.replay.targeting import (
    LocatorAttempt,
    TargetAmbiguous,
    TargetResolutionError,
    TargetResolver,
)
from interface_cua.replay.waits import ConditionChecker, route_match
from interface_cua.schema.artifact import (
    ApprovalState,
    Capability,
    InputSpec,
    InputValue,
    PostconditionBranch,
    RiskLevel,
    Step,
    ValueType,
)
from interface_cua.schema.result import (
    BusinessOutcomeResult,
    FailureCategory,
    FailureResult,
    NeedsHumanResult,
    ReplayResult,
    SuccessResult,
    UnknownSideEffectResult,
    ValidationCheck,
    ValidationRequiredResult,
)
from interface_cua.surface.base import SurfaceAdapter


class ReplayExecutor:
    def __init__(
        self,
        surface: SurfaceAdapter,
        policy: PolicyEngine,
        *,
        application_family: str,
        application_version: str,
        timeout_ms: int = 10_000,
        allow_draft: bool = False,
        evidence: EvidenceWriter | None = None,
    ) -> None:
        self.surface = surface
        self.policy = policy
        self.application_family = application_family
        self.application_version = application_version
        self.timeout_ms = timeout_ms
        self.allow_draft = allow_draft
        self.evidence = evidence
        self.resolver = TargetResolver(surface)
        self.checker = ConditionChecker(surface, self.resolver)
        self.reconciler = Reconciler(
            surface, self.checker, policy, timeout_ms=timeout_ms, evidence=evidence
        )

    async def execute(
        self,
        artifact: Capability,
        arguments: dict[str, object],
        *,
        confirmed_steps: frozenset[str] = frozenset(),
        resume_from: str | None = None,
    ) -> ReplayResult:
        """Run the capability and, on any non-success terminal state, leave a failure bundle.

        `resume_from` restarts at a named step after a human held the session. It is not a "skip
        ahead" switch: the resumed step verifies its own precondition like any other, so a human
        who left the session somewhere unexpected stops the run instead of silently continuing
        from a step number that no longer means anything.
        """

        result = await self._execute(artifact, arguments, confirmed_steps, resume_from)
        if self.evidence is None:
            return result
        bundle = await self.evidence.capture_failure(self.surface, result)
        if bundle is not None and isinstance(result, FailureResult):
            return result.model_copy(update={"evidence": str(bundle)})
        return result

    async def _execute(
        self,
        artifact: Capability,
        arguments: dict[str, object],
        confirmed_steps: frozenset[str],
        resume_from: str | None = None,
    ) -> ReplayResult:
        # Entry checks are exactly that. On a resume the session is deliberately mid-flow, so
        # re-asserting the entry route and landmarks would fail every resume — and would mask the
        # precondition check that is the real gate on whether a human left things usable.
        validation = await self._validate_artifact(artifact, entry_checks=resume_from is None)
        if validation is not None:
            return validation
        try:
            inputs = _validate_inputs(artifact.inputs, arguments)
        except ValueError as exc:
            return FailureResult(
                category=FailureCategory.PRECONDITION_FAILED,
                retryable=False,
                step="input-validation",
                expected="arguments matching the capability input schema",
                observed=str(exc),
            )

        outputs: dict[str, Any] = {}
        steps = artifact.steps
        if resume_from is not None:
            names = [step.id for step in steps]
            if resume_from not in names:
                return FailureResult(
                    category=FailureCategory.PRECONDITION_FAILED,
                    retryable=False,
                    step=resume_from,
                    expected=f"a step of {artifact.capability.id}",
                    observed=f"no such step; declared steps are {names}",
                )
            steps = steps[names.index(resume_from) :]

        for index, step in enumerate(steps):
            result = await self._run_step_with_retry(
                artifact, step, inputs, confirmed_steps, index
            )
            if isinstance(result, UnknownSideEffectResult) and artifact.reconciliation is not None:
                # The write may or may not have happened. The one thing that must not happen now
                # is another attempt at it (invariant 4) — so look, don't retry.
                return await self.reconciler.resolve(
                    artifact.reconciliation, result, inputs, outputs
                )
            if result is not None:
                # A declared business outcome is still a successful invocation, so anything the
                # capability already promised and harvested belongs in the answer. Attached here
                # rather than threaded through every step signature.
                if isinstance(result, BusinessOutcomeResult) and outputs:
                    return result.model_copy(update={"outputs": dict(outputs)})
                return result
            harvest_failure = await self._harvest_outputs(artifact, step, outputs)
            if harvest_failure is not None:
                return harvest_failure
        return SuccessResult(outputs=outputs)

    async def _run_step_with_retry(
        self,
        artifact: Capability,
        step: Step,
        inputs: dict[str, object],
        confirmed_steps: frozenset[str],
        step_index: int,
    ) -> ReplayResult | None:
        """Repeat a step only when the artifact declared it safe to repeat.

        `retry.safe` is the author's assertion that running this step twice cannot double a side
        effect. Without it a step runs exactly once, whatever `max_attempts` says — which is how
        invariant 4 survives contact with a flaky UI.
        """

        attempts = step.retry.max_attempts if step.retry.safe else 1
        first: ReplayResult | None = None
        for attempt in range(attempts):
            if attempt > 0 and not await self._precondition_holds(step, inputs):
                # The action landed and moved the session on; only the checkpoint was missed.
                # Re-running from a different screen would replace a real diagnosis with a
                # meaningless precondition error, so report what actually went wrong.
                return first
            if attempt > 0 and self.evidence is not None:
                self.evidence.notice(
                    NoticeKind.STEP_RETRIED, step=step.id, attempt=attempt + 1, of=attempts
                )
            result = await self._observed_step(
                artifact, step, inputs, step.id in confirmed_steps, step_index
            )
            if attempt == 0:
                first = result
            if not _is_retryable(result, step) or attempt == attempts - 1:
                return result
        return first

    async def _observed_step(
        self,
        artifact: Capability,
        step: Step,
        inputs: dict[str, object],
        human_confirmed: bool,
        step_index: int,
    ) -> ReplayResult | None:
        """Run one step and record what was decided and why.

        Replay's events carry `decision_source: "artifact"`, and the event schema forbids a model
        rationale on those — so invariant 1 is legible in the evidence, not just in the README.
        """

        if self.evidence is None:
            return await self._execute_step(artifact, step, inputs, human_confirmed, _StepTrace())

        trace = _StepTrace()
        observation = await self.evidence.observe(
            self.surface, screenshot_as=f"{step_index:03d}-{step.id}.png"
        )
        started = time.monotonic()
        result = await self._execute_step(artifact, step, inputs, human_confirmed, trace)
        elapsed_ms = int((time.monotonic() - started) * 1000)

        self.evidence.emit(
            RunEvent(
                run_id=self.evidence.run_id,
                step_index=step_index,
                step_id=step.id,
                decision_source="artifact",
                observation=observation,
                proposed_action=ProposedAction(
                    type=step.action.type,
                    value_ref=step.action.value.from_input if step.action.value else None,
                ),
                policy_decision=trace.policy,
                target=trace.target,
                result=StepResult(
                    ok=result is None,
                    elapsed_ms=elapsed_ms,
                    postcondition=trace.postcondition,
                    outcome=None if result is None else result.status,
                ),
            )
        )
        return result

    def _navigation_destination(self, step: Step, inputs: dict[str, object]) -> str | None:
        """Where a navigate action would land, absolutised so policy can judge its origin."""

        if step.action.type != "navigate" or step.action.value is None:
            return None
        return urljoin(self.surface.current_url, _input_value(step.action.value, inputs))

    async def _precondition_holds(self, step: Step, inputs: dict[str, object]) -> bool:
        try:
            return await self.checker.matches(step.precondition, inputs)
        except TargetResolutionError:
            return False

    async def _execute_step(
        self,
        artifact: Capability,
        step: Step,
        inputs: dict[str, object],
        human_confirmed: bool,
        trace: _StepTrace,
    ) -> ReplayResult | None:
        try:
            precondition_matches = await self.checker.wait_for(
                step.precondition, inputs, self.timeout_ms
            )
        except TargetAmbiguous as exc:
            return _target_failure(step, exc)
        if not precondition_matches:
            return FailureResult(
                category=FailureCategory.PRECONDITION_FAILED,
                retryable=False,
                step=step.id,
                expected=f"precondition {step.precondition.type}",
                observed=(await self.surface.page_text())[:500],
            )

        decision = self.policy.authorize(
            step.action,
            step.risk,
            AuthorizationContext(
                self.surface.current_url,
                human_confirmed=human_confirmed,
                destination_url=self._navigation_destination(step, inputs),
            ),
        )
        trace.policy = PolicyDecisionEvent(
            verdict=decision.verdict.value, rule=decision.rule, origin_ok=decision.origin_ok
        )
        if decision.verdict == PolicyVerdict.REQUIRE_HUMAN:
            return NeedsHumanResult(reason=decision.rule, step=step.id)
        if decision.verdict == PolicyVerdict.DENY:
            return FailureResult(
                category=FailureCategory.POLICY_DENIED,
                retryable=False,
                step=step.id,
                expected="policy authorization",
                observed=decision.rule,
            )

        try:
            await self._act(step, inputs, trace)
            await self.surface.wait_until_settled(self.timeout_ms)
        except TargetResolutionError as exc:
            trace.target = _target_event(step, exc.attempts, unique=False)
            # The target may be absent precisely because the application took one of the
            # alternatives this step declared. Consult the contract before calling it a failure,
            # but never let a *success* branch match here: the action did not run.
            branch = await self._matching_branch(step, inputs, success_allowed=False)
            if branch is not None:
                return _disposition(branch, step)
            return _target_failure(step, exc, (await self.surface.page_text())[:500])
        except LeaseViolation:
            # Another controller holds the session. Never force it (invariant 5).
            raise
        # Surface implementations expose different transport exceptions; this is the safety
        # boundary that turns all of them into typed terminal results.
        except Exception as exc:  # noqa: BLE001
            if step.risk.level == RiskLevel.CONSEQUENTIAL_WRITE:
                return UnknownSideEffectResult(step=step.id, reconciliation="pending")
            return FailureResult(
                category=FailureCategory.APPLICATION_ERROR,
                retryable=step.retry.safe,
                step=step.id,
                expected="action completed and surface settled",
                observed=f"{type(exc).__name__}: {exc}",
            )

        try:
            branch = await self._await_branch(artifact, step, inputs)
        except TargetAmbiguous as exc:
            return _target_failure(step, exc)
        if branch is not None:
            trace.postcondition = branch.name
            return _disposition(branch, step)

        # Nothing the capability declared describes this screen. That is the definition of a
        # system failure here, and the executor deliberately does not try to name it from page
        # copy — naming it would put application-specific interpretation below the model.
        if step.risk.level == RiskLevel.CONSEQUENTIAL_WRITE:
            return UnknownSideEffectResult(step=step.id, reconciliation="pending")
        return FailureResult(
            category=FailureCategory.POSTCONDITION_FAILED,
            retryable=False,
            step=step.id,
            expected="one of the declared postconditions: "
            + ", ".join(branch.name for branch in step.postcondition.any_of),
            observed=(await self.surface.page_text())[:500],
        )

    async def _await_branch(
        self, artifact: Capability, step: Step, inputs: dict[str, object]
    ) -> PostconditionBranch | None:
        """Wait for one declared postcondition, dismissing declared interstitials on the way.

        Recovery lives here because this is where "the screen is not what I expected yet" is
        actually detected. Only interstitials the artifact named are dismissed, once each per step.
        """

        dismissed: set[str] = set()

        async def probe() -> PostconditionBranch | None:
            branch = await self._matching_branch(step, inputs, success_allowed=True)
            if branch is not None:
                return branch
            await self._dismiss_interstitials(artifact, dismissed)
            return None

        return await poll_until(probe, self.timeout_ms)

    async def _dismiss_interstitials(self, artifact: Capability, dismissed: set[str]) -> bool:
        for interstitial in artifact.interstitials:
            if interstitial.name in dismissed:
                continue
            try:
                await self.resolver.resolve_once(interstitial.detect)
                control = await self.resolver.resolve_once(interstitial.dismiss)
            except TargetResolutionError:
                continue
            dismissed.add(interstitial.name)
            await control.element.click()
            await self.surface.wait_until_settled(self.timeout_ms)
            if self.evidence is not None:
                self.evidence.notice(
                    NoticeKind.INTERSTITIAL_DISMISSED, interstitial=interstitial.name
                )
            return True
        return False

    async def _matching_branch(
        self, step: Step, inputs: dict[str, object], *, success_allowed: bool
    ) -> PostconditionBranch | None:
        for branch in step.postcondition.any_of:
            if branch.is_success and not success_allowed:
                continue
            try:
                matched = await self.checker.matches(branch.condition, inputs)
            except TargetAmbiguous:
                if success_allowed:
                    raise
                continue
            if matched:
                return branch
        return None

    async def _act(self, step: Step, inputs: dict[str, object], trace: _StepTrace) -> None:
        action = step.action
        element = None
        if step.target is not None:
            resolved = await self.resolver.resolve(step.target)
            trace.target = _target_event(step, list(resolved.attempts), unique=True)
            element = resolved.element
        value = None if action.value is None else _input_value(action.value, inputs)
        if action.type == "click":
            await element.click()  # type: ignore[union-attr]
        elif action.type == "fill":
            await element.fill(value)  # type: ignore[union-attr,arg-type]
        elif action.type == "select":
            await element.select(value)  # type: ignore[union-attr,arg-type]
        elif action.type == "keypress":
            await self.surface.keypress(action.key)  # type: ignore[arg-type]
        elif action.type == "navigate":
            await self.surface.navigate(value)  # type: ignore[arg-type]
        elif action.type == "extract":
            await element.text()  # type: ignore[union-attr]

    async def _harvest_outputs(
        self, artifact: Capability, step: Step, outputs: dict[str, Any]
    ) -> FailureResult | None:
        """Extract the outputs pinned to this step, or fail. Never return a promise unfulfilled."""

        for output in artifact.outputs:
            if output.after_step != step.id:
                continue
            try:
                text = await (await self.resolver.resolve(output.extraction)).element.text()
            except TargetResolutionError as exc:
                return _target_failure_for_output(output.name, step, exc)
            try:
                value = _coerce_output(output.type, text, output.enum_values)
            except ValueError as exc:
                return FailureResult(
                    category=FailureCategory.INVALID_OUTPUT,
                    retryable=False,
                    step=step.id,
                    expected=f"output {output.name} of type {output.type.value}",
                    observed=f"{exc}: {text[:200]!r}",
                )
            if output.max_length is not None and len(str(value)) > output.max_length:
                return FailureResult(
                    category=FailureCategory.INVALID_OUTPUT,
                    retryable=False,
                    step=step.id,
                    expected=f"output {output.name} within {output.max_length} characters",
                    observed=f"{len(str(value))} characters",
                )
            outputs[output.name] = value
        return None

    async def _validate_artifact(
        self, artifact: Capability, *, entry_checks: bool = True
    ) -> ValidationRequiredResult | None:
        """Refuse to start unless this really is the approved capability on the expected app.

        The family/version pair is what the caller *asserts*; the route and landmark checks are
        what the live page actually shows. Only the second kind can catch an application that
        changed under a capability authored against it.
        """

        if artifact.approval_state != ApprovalState.APPROVED and not self.allow_draft:
            return ValidationRequiredResult(
                check=ValidationCheck.APPROVAL,
                reason="capability has not been approved for replay",
                expected={"approval_state": ApprovalState.APPROVED.value},
                observed={"approval_state": artifact.approval_state.value},
            )

        app = artifact.application
        if (
            app.family != self.application_family
            or self.application_version not in app.supported_versions
        ):
            return ValidationRequiredResult(
                check=ValidationCheck.APPLICATION,
                reason="capability was authored for a different application or version",
                expected={
                    "family": app.family,
                    "supported_versions": app.supported_versions,
                },
                observed={
                    "family": self.application_family,
                    "version": self.application_version,
                },
            )

        if not entry_checks:
            return None

        entry_url = self.surface.current_url
        if not any(route_match(pattern, entry_url) for pattern in app.fingerprint.route_patterns):
            return ValidationRequiredResult(
                check=ValidationCheck.ENTRY_ROUTE,
                reason="session is not on a route this capability was authored against",
                expected={"route_patterns": app.fingerprint.route_patterns},
                observed={"url": entry_url},
            )

        page_text = await self.surface.page_text()
        missing = [name for name in app.fingerprint.entry_landmarks if name not in page_text]
        if missing:
            return ValidationRequiredResult(
                check=ValidationCheck.ENTRY_LANDMARKS,
                reason="entry screen is missing landmarks the capability depends on",
                expected={"entry_landmarks": app.fingerprint.entry_landmarks},
                observed={"missing": missing},
            )
        return None


@dataclass(slots=True)
class _StepTrace:
    """What a step decided, collected for the event log without changing control flow."""

    policy: PolicyDecisionEvent | None = None
    target: TargetEvent | None = None
    postcondition: str | None = None


def _target_event(step: Step, attempts: list[LocatorAttempt], *, unique: bool) -> TargetEvent:
    return TargetEvent(
        frame=step.target.frame if step.target else None,
        strategy=attempts[-1].strategy if attempts else None,
        unique=unique,
        attempts=[
            {
                "strategy": attempt.strategy,
                "matches": attempt.matches,
                "rejected_because": attempt.rejected_because,
            }
            for attempt in attempts
        ],
    )


def _input_value(reference: InputValue, inputs: dict[str, object]) -> str:
    name = reference.from_input.removeprefix("${inputs.").removesuffix("}")
    return str(inputs[name])


_RETRYABLE_CATEGORIES = frozenset(
    {
        FailureCategory.TARGET_NOT_FOUND,
        FailureCategory.POSTCONDITION_FAILED,
        FailureCategory.APPLICATION_ERROR,
    }
)


def _is_retryable(result: ReplayResult | None, step: Step) -> bool:
    """Only transient, side-effect-free failures are worth a second attempt.

    A business outcome, an escalation, a policy denial, an ambiguous target or a bad output are all
    settled answers — repeating the step cannot change any of them, and pretending otherwise would
    turn a clean result into a retry storm.
    """

    if result is None or not step.retry.safe:
        return False
    return (
        isinstance(result, FailureResult) and result.category in _RETRYABLE_CATEGORIES
    )


def _disposition(branch: PostconditionBranch, step: Step) -> ReplayResult | None:
    """Turn a matched declared branch into its terminal result, or ``None`` to continue."""

    if branch.business_outcome is not None:
        return BusinessOutcomeResult(code=branch.business_outcome, step=step.id)
    if branch.escalation is not None:
        return NeedsHumanResult(reason=branch.escalation, step=step.id)
    return None


def _validate_inputs(specs: list[InputSpec], arguments: dict[str, object]) -> dict[str, object]:
    declared = {spec.name: spec for spec in specs}
    extras = set(arguments) - set(declared)
    if extras:
        raise ValueError(f"unexpected inputs: {sorted(extras)}")
    result: dict[str, object] = {}
    for name, spec in declared.items():
        if name not in arguments:
            if spec.required:
                raise ValueError(f"missing required input: {name}")
            continue
        value = arguments[name]
        if spec.type in {ValueType.STRING, ValueType.ENUM}:
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            if spec.min_length is not None and len(value) < spec.min_length:
                raise ValueError(f"{name} is shorter than min_length")
            if spec.max_length is not None and len(value) > spec.max_length:
                raise ValueError(f"{name} is longer than max_length")
            if spec.pattern is not None and re.fullmatch(spec.pattern, value) is None:
                raise ValueError(f"{name} does not match its pattern")
            if spec.enum_values is not None and value not in spec.enum_values:
                raise ValueError(f"{name} is not an allowed enum value")
        elif spec.type == ValueType.INTEGER and not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        elif spec.type == ValueType.BOOLEAN and not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean")
        elif spec.type == ValueType.DECIMAL:
            try:
                Decimal(str(value))
            except InvalidOperation as exc:
                raise ValueError(f"{name} must be a decimal") from exc
        result[name] = value
    return result


def _coerce_output(type_: ValueType, text: str, enum_values: list[str] | None) -> Any:
    normalized = text.strip()
    if type_ == ValueType.DECIMAL:
        try:
            return str(Decimal(normalized.replace("$", "").replace(",", "")))
        except InvalidOperation as exc:
            raise ValueError("invalid decimal output") from exc
    if type_ == ValueType.INTEGER:
        return int(normalized)
    if type_ == ValueType.BOOLEAN:
        if normalized.lower() not in {"true", "false"}:
            raise ValueError("invalid boolean output")
        return normalized.lower() == "true"
    if type_ == ValueType.ENUM and (enum_values is None or normalized not in enum_values):
        raise ValueError("invalid enum output")
    return normalized


def _target_failure(
    step: Step, exc: TargetResolutionError, observed_text: str = ""
) -> FailureResult:
    category = (
        FailureCategory.TARGET_AMBIGUOUS
        if isinstance(exc, TargetAmbiguous)
        else FailureCategory.TARGET_NOT_FOUND
    )
    observed = f"{exc}; page showed: {observed_text}" if observed_text else str(exc)
    return FailureResult(
        category=category,
        retryable=False,
        step=step.id,
        expected="a unique semantic target",
        observed=observed,
        locator_attempts=_locator_attempt_payload(exc),
    )


def _target_failure_for_output(
    output_name: str, step: Step, exc: TargetResolutionError
) -> FailureResult:
    return FailureResult(
        category=FailureCategory.INVALID_OUTPUT,
        retryable=False,
        step=step.id,
        expected=f"a unique extraction target for declared output {output_name}",
        observed=str(exc),
        locator_attempts=_locator_attempt_payload(exc),
    )


def _locator_attempt_payload(exc: TargetResolutionError) -> list[dict[str, object]]:
    return [
        {
            "strategy": attempt.strategy,
            "matches": attempt.matches,
            "rejected_because": attempt.rejected_because,
        }
        for attempt in exc.attempts
    ]
