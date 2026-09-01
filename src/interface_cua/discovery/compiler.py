"""Discovery run → capability artifact.

The compiler is where a one-off trace becomes something reusable, and its central job is
**parameterisation**: the recorder saw `/member/58431`, and the artifact must say `/member/:id`
bound to `${inputs.member_id}`. Without that the capability only ever works for the member it was
discovered on (plan §6).

What it deliberately does *not* do:

- **Invent business outcomes.** A happy-path run only ever sees the happy path, so the compiler
  emits the success branch and nothing else. `postcondition.any_of` is the outcome contract, and a
  contract cannot be inferred from a single observation — a human adds the declared outcomes while
  reviewing the draft. That is what `approval_state: draft` is for.
- **Guess consequential risk.** Nothing here ever emits `consequential_write`. Under-calling risk
  is the dangerous direction, so the compiler stays conservative and a reviewer raises it.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from interface_cua.discovery.orchestrator import DiscoveryOutcome, DiscoveryRun, RecordedStep
from interface_cua.schema.artifact import (
    ActionSpec,
    ApplicationFingerprint,
    ApplicationSpec,
    Capability,
    CapabilityIdentity,
    InputSpec,
    InputValue,
    OutputSpec,
    Postcondition,
    PostconditionBranch,
    Provenance,
    RetrySpec,
    RiskLevel,
    RiskSpec,
    RouteCondition,
    Step,
    TargetSpec,
    ValueType,
)


class CompilationError(RuntimeError):
    """The run cannot honestly be expressed as a capability."""


def compile_run(
    run: DiscoveryRun,
    *,
    capability_id: str,
    description: str,
    inputs: dict[str, str],
    application_family: str,
    application_version: str,
    entry_landmarks: list[str],
    model_id: str,
    operator: str,
    version: str = "1.0.0",
) -> Capability:
    """Turn a finished discovery run into a draft `Capability`.

    `inputs` maps input name → the literal value used during discovery. That mapping is what makes
    canonicalisation possible: every occurrence of a value becomes a reference to its name.
    """

    if run.outcome is not DiscoveryOutcome.FINISHED:
        raise CompilationError(
            f"run ended as {run.outcome.value}, not finished — refusing to compile a partial trace"
        )
    if not run.steps:
        raise CompilationError("run recorded no steps")

    binder = _Binder(inputs)
    fused = _fuse_focus_then_type(run.steps)
    steps = [_compile_step(step, index, binder) for index, step in enumerate(fused)]

    return Capability(
        capability=CapabilityIdentity(id=capability_id, version=version, description=description),
        application=ApplicationSpec(
            family=application_family,
            supported_versions=[application_version],
            fingerprint=ApplicationFingerprint(
                route_patterns=sorted({s.precondition.pattern for s in steps}  # type: ignore[union-attr]
                                      | {binder.canonicalise(run.steps[-1].url_after)[0]}),
                entry_landmarks=entry_landmarks,
            ),
        ),
        inputs=[_input_spec(name, value) for name, value in inputs.items()],
        outputs=[
            _output_spec(recorded, step_id)
            for recorded in run.outputs.values()
            if recorded.target is not None
            and (step_id := _step_visible_on(recorded.captured_at_url, fused, steps)) is not None
        ],
        steps=steps,
        provenance=Provenance(
            discovery_run_id=run.run_id,
            model_id=model_id,
            timestamp=datetime.now(UTC),
            operator=operator,
        ),
    )


def _step_visible_on(url: str, fused: list[RecordedStep], steps: list[Step]) -> str | None:
    """The id of the last step that leaves the session on `url`.

    An output is only extractable where it is rendered, so pinning it anywhere else guarantees an
    INVALID_OUTPUT at replay time. Returns None when no step lands there, in which case the output
    is dropped rather than promised.
    """

    for recorded, compiled in zip(reversed(fused), reversed(steps), strict=True):
        if recorded.url_after == url:
            return compiled.id
    return None


def _fuse_focus_then_type(steps: list[RecordedStep]) -> list[RecordedStep]:
    """Collapse "click a field, then type" into a single `fill` of that field.

    Computer-use `type` has no target of its own — it types wherever focus happens to be. The
    artifact's `fill` needs one, and the click immediately before is precisely what gave the field
    focus. Fusing them turns two coordinate-level actions into one semantic step, which is the
    level a reviewer should be reading.
    """

    fused: list[RecordedStep] = []
    for step in steps:
        previous = fused[-1] if fused else None
        if (
            step.action.kind == "type"
            and previous is not None
            and previous.action.kind == "click"
            and previous.target is not None
            and previous.url_after == step.url_before
        ):
            fused[-1] = replace(
                previous,
                action=step.action,
                url_after=step.url_after,
                rationale=step.rationale or previous.rationale,
            )
            continue
        fused.append(step)
    return fused


class _Binder:
    """Replaces literal input values with `${inputs.name}` wherever they appear."""

    def __init__(self, inputs: dict[str, str]) -> None:
        # Longest first, so a value that contains another does not get half-replaced.
        self.pairs = sorted(inputs.items(), key=lambda kv: len(kv[1]), reverse=True)

    def canonicalise(self, url: str) -> tuple[str, dict[str, str]]:
        """`/member/58431` → (`/member/:member_id`, {member_id: '${inputs.member_id}'})."""

        path = urlparse(url).path
        bindings: dict[str, str] = {}
        for name, value in self.pairs:
            if value and value in path:
                path = path.replace(value, f":{name}")
                bindings[name] = f"${{inputs.{name}}}"
        return path, bindings

    def reference(self, text: str | None) -> str | None:
        """The input name a typed value corresponds to, if any."""

        if text is None:
            return None
        for name, value in self.pairs:
            if value and value == text.strip():
                return name
        return None


def _compile_step(step: RecordedStep, index: int, binder: _Binder) -> Step:
    before_pattern, before_bindings = binder.canonicalise(step.url_before)
    after_pattern, after_bindings = binder.canonicalise(step.url_after)

    action, target = _action_and_target(step, binder)
    return Step(
        id=_step_id(step, index),
        precondition=RouteCondition(
            type="route", pattern=before_pattern, bindings=before_bindings
        ),
        action=action,
        target=target,
        postcondition=Postcondition(
            any_of=[
                PostconditionBranch(
                    name="step-completed",
                    condition=RouteCondition(
                        type="route", pattern=after_pattern, bindings=after_bindings
                    ),
                )
            ]
        ),
        retry=_retry_for(step),
        risk=_risk_for(step),
    )


def _action_and_target(step: RecordedStep, binder: _Binder) -> tuple[ActionSpec, TargetSpec | None]:
    kind = step.action.kind
    target = step.target.target if step.target else None

    if kind == "type":
        name = binder.reference(step.action.text)
        if name is None:
            raise CompilationError(
                f"step {step.index} typed a literal that is not a declared input: "
                f"{(step.action.text or '')[:20]!r}. A capability must not hard-code run data."
            )
        return ActionSpec(type="fill", value=InputValue(from_input=f"${{inputs.{name}}}")), target
    if kind == "key":
        return ActionSpec(type="keypress", key=step.action.text or "Enter"), None
    if kind == "click":
        if target is None:
            raise CompilationError(
                f"step {step.index} clicked at a point the recorder could not resolve; "
                "a coordinate must never reach the artifact"
            )
        return ActionSpec(type="click"), target
    raise CompilationError(f"step {step.index}: {kind!r} has no artifact equivalent")


def _step_id(step: RecordedStep, index: int) -> str:
    """A readable id from what the step touched, not just its position."""

    label = (step.target.accessible_name if step.target else None) or step.action.kind
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "step"
    if not slug[0].isalpha():
        slug = f"s-{slug}"
    return f"{slug}-{index}"


def _retry_for(step: RecordedStep) -> RetrySpec:
    """Only steps that did not change the page are marked safe to repeat (invariant 4)."""

    navigated = step.url_before != step.url_after
    return RetrySpec(max_attempts=1, safe=not navigated)


def _risk_for(step: RecordedStep) -> RiskSpec:
    """Conservative by construction. `consequential_write` is never inferred, only reviewed in."""

    navigated = step.url_before != step.url_after
    level = RiskLevel.REVERSIBLE_WRITE if navigated else RiskLevel.READ
    return RiskSpec(level=level, requires_confirmation=False)


def _input_spec(name: str, value: str) -> InputSpec:
    if value.isdigit():
        return InputSpec(
            name=name,
            type=ValueType.STRING,
            description=f"Discovered from a run using a {len(value)}-digit reference.",
            # Identifier-shaped inputs are treated as sensitive by default; a reviewer can relax
            # it, but the default must not be the leaky one.
            sensitive=True,
            min_length=len(value),
            max_length=len(value),
            pattern=rf"^\d{{{len(value)}}}$",
        )
    return InputSpec(
        name=name,
        type=ValueType.STRING,
        description="Discovered input.",
        sensitive=False,
        max_length=200,
    )


def _output_spec(recorded: Any, after_step: str) -> OutputSpec:
    value_type = _infer_type(recorded.observed_value)
    return OutputSpec(
        name=recorded.name,
        type=value_type,
        description=f"Captured during discovery from {recorded.target.role or 'the page'}.",
        after_step=after_step,
        extraction=recorded.target.target,
        max_length=200 if value_type is ValueType.STRING else None,
    )


def _infer_type(value: str) -> ValueType:
    cleaned = value.replace("$", "").replace(",", "").strip()
    try:
        Decimal(cleaned)
    except InvalidOperation:
        return ValueType.STRING
    return ValueType.DECIMAL if "." in cleaned else ValueType.STRING
