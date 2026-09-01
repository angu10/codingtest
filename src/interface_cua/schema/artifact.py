"""Human-reviewable capability artifact schema.

This module is the source of truth. YAML artifacts and ``schema.json`` are projections of these
Pydantic models; engines must consume the models rather than loosely-shaped dictionaries.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

SEMVER_PATTERN = r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$"
INPUT_REF_PATTERN = re.compile(r"^\$\{inputs\.([a-z][a-z0-9_]*)\}$")
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]*$")]
InputName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]
OutcomeCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")]


class StrictModel(BaseModel):
    """Reject schema drift at every artifact boundary."""

    model_config = ConfigDict(extra="forbid")


class ApprovalState(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"


class ValueType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    ENUM = "enum"


class CapabilityIdentity(StrictModel):
    id: Identifier
    version: str = Field(pattern=SEMVER_PATTERN)
    description: str = Field(min_length=1, max_length=500)


class ApplicationFingerprint(StrictModel):
    """What replay checks before it touches anything, to know it is on the app it was authored for.

    Both fields are verifiable at the capability's *entry point* — a fingerprint that could only be
    confirmed halfway through the flow would be checked too late to prevent anything.
    """

    #: Route templates this capability traverses. The entry URL must match one of them.
    route_patterns: list[str] = Field(min_length=1)
    #: Accessible names that must be present on the entry screen.
    entry_landmarks: list[str] = Field(min_length=1)


class ApplicationSpec(StrictModel):
    family: Identifier
    supported_versions: list[str] = Field(min_length=1)
    fingerprint: ApplicationFingerprint


class InputSpec(StrictModel):
    name: InputName
    type: ValueType
    description: str = Field(min_length=1, max_length=300)
    required: bool = True
    sensitive: bool = False
    enum_values: list[str] | None = None
    min_length: int | None = Field(default=None, ge=0)
    max_length: int | None = Field(default=None, ge=1)
    pattern: str | None = None

    @model_validator(mode="after")
    def validate_constraints(self) -> InputSpec:
        if self.type == ValueType.ENUM and not self.enum_values:
            raise ValueError("enum inputs require enum_values")
        if self.type != ValueType.ENUM and self.enum_values is not None:
            raise ValueError("enum_values is valid only for enum inputs")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("min_length cannot exceed max_length")
        if self.pattern is not None:
            re.compile(self.pattern)
        return self


class AccessibilityStrategy(StrictModel):
    type: Literal["accessibility"]
    role: str = Field(min_length=1)
    name: str = Field(min_length=1)
    exact: bool = True


class TextStrategy(StrictModel):
    type: Literal["text"]
    value: str = Field(min_length=1)
    exact: bool = True


class RelativeStrategy(StrictModel):
    type: Literal["relative"]
    anchor: str = Field(min_length=1)
    role: str = Field(min_length=1)
    name: str | None = None
    relation: Literal["same_row", "following_cell"] = "same_row"


TargetStrategy = Annotated[
    AccessibilityStrategy | TextStrategy | RelativeStrategy,
    Field(discriminator="type"),
]


class TargetSpec(StrictModel):
    frame: str | None = None
    strategies: list[TargetStrategy] = Field(min_length=1)
    # A capability may never opt out of invariant 2.
    must_be_unique: Literal[True] = True


class PageCondition(StrictModel):
    type: Literal["page"]
    name: Identifier
    landmark: TargetSpec


class RouteCondition(StrictModel):
    type: Literal["route"]
    pattern: str = Field(min_length=1)
    bindings: dict[InputName, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_bindings(self) -> RouteCondition:
        for reference in self.bindings.values():
            if INPUT_REF_PATTERN.fullmatch(reference) is None:
                raise ValueError(f"route binding must be an input reference: {reference}")
        return self


class TargetStateCondition(StrictModel):
    type: Literal["target_state"]
    target: TargetSpec
    state: Literal["visible", "hidden", "enabled", "disabled"]


class TextCondition(StrictModel):
    type: Literal["text"]
    value: str = Field(min_length=1, max_length=500)


Condition = Annotated[
    PageCondition | RouteCondition | TargetStateCondition | TextCondition,
    Field(discriminator="type"),
]


class PostconditionBranch(StrictModel):
    """One declared way a step is allowed to end.

    A branch with neither ``business_outcome`` nor ``escalation`` is a success branch. Anything the
    capability does *not* declare here is a system failure by definition — that rule is what keeps
    the business-outcome taxonomy a property of this contract rather than a judgement made by the
    executor over page copy.
    """

    name: Identifier
    condition: Condition
    business_outcome: OutcomeCode | None = None
    escalation: OutcomeCode | None = None

    @model_validator(mode="after")
    def validate_disposition(self) -> PostconditionBranch:
        if self.business_outcome is not None and self.escalation is not None:
            raise ValueError("a branch is either a business outcome or an escalation, never both")
        return self

    @property
    def is_success(self) -> bool:
        return self.business_outcome is None and self.escalation is None


class Postcondition(StrictModel):
    any_of: list[PostconditionBranch] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_branches(self) -> Postcondition:
        names = [branch.name for branch in self.any_of]
        if len(names) != len(set(names)):
            raise ValueError("postcondition branch names must be unique")
        outcomes = [branch.business_outcome for branch in self.any_of if branch.business_outcome]
        if len(outcomes) != len(set(outcomes)):
            raise ValueError("business outcome codes must be unique within a step")
        escalations = [branch.escalation for branch in self.any_of if branch.escalation]
        if len(escalations) != len(set(escalations)):
            raise ValueError("escalation codes must be unique within a step")
        if not any(branch.is_success for branch in self.any_of):
            raise ValueError("postcondition must contain at least one success branch")
        return self


class InputValue(StrictModel):
    from_input: str

    @model_validator(mode="after")
    def validate_reference(self) -> InputValue:
        if INPUT_REF_PATTERN.fullmatch(self.from_input) is None:
            raise ValueError("from_input must have the form ${inputs.name}")
        return self


class ActionSpec(StrictModel):
    type: Literal["click", "fill", "select", "keypress", "navigate", "extract"]
    value: InputValue | None = None
    key: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> ActionSpec:
        if self.type in {"fill", "select", "navigate"} and self.value is None:
            raise ValueError(f"{self.type} actions require value")
        if self.type == "keypress" and not self.key:
            raise ValueError("keypress actions require key")
        if self.type not in {"fill", "select", "navigate"} and self.value is not None:
            raise ValueError(f"{self.type} actions cannot contain value")
        if self.type != "keypress" and self.key is not None:
            raise ValueError(f"{self.type} actions cannot contain key")
        return self


class RetrySpec(StrictModel):
    max_attempts: int = Field(default=1, ge=1, le=5)
    safe: bool = False


class RiskLevel(StrEnum):
    READ = "read"
    REVERSIBLE_WRITE = "reversible_write"
    CONSEQUENTIAL_WRITE = "consequential_write"


class RiskSpec(StrictModel):
    level: RiskLevel
    requires_confirmation: bool = False

    @model_validator(mode="after")
    def consequential_requires_confirmation(self) -> RiskSpec:
        if self.level == RiskLevel.CONSEQUENTIAL_WRITE and not self.requires_confirmation:
            raise ValueError("consequential writes must require confirmation")
        return self


class Interstitial(StrictModel):
    """A known, recoverable screen the capability is allowed to dismiss.

    Declaring these keeps recovery inside the contract: replay dismisses only interstitials the
    artifact named, once each per step, and never improvises its way past an unexpected screen.
    """

    name: Identifier
    detect: TargetSpec
    dismiss: TargetSpec


class Step(StrictModel):
    id: Identifier
    precondition: Condition
    action: ActionSpec
    target: TargetSpec | None = None
    postcondition: Postcondition
    retry: RetrySpec = Field(default_factory=RetrySpec)
    risk: RiskSpec

    @model_validator(mode="after")
    def validate_safety(self) -> Step:
        target_required = self.action.type in {"click", "fill", "select", "extract"}
        if target_required and self.target is None:
            raise ValueError(f"{self.action.type} actions require a target")
        if self.action.type == "navigate" and self.target is not None:
            raise ValueError("navigate actions cannot contain a target")
        if self.risk.level == RiskLevel.CONSEQUENTIAL_WRITE and (
            self.retry.safe or self.retry.max_attempts != 1
        ):
            raise ValueError("consequential writes must be single-attempt and unsafe to retry")
        return self


class OutputSpec(StrictModel):
    """A typed value the capability promises to return.

    ``after_step`` pins extraction to the point in the flow where the value is actually on screen,
    which makes harvesting part of the decision graph rather than opportunistic scraping. A promised
    output that cannot be extracted or coerced there is a failure, never a silently absent key.
    """

    name: InputName
    type: ValueType
    description: str = Field(min_length=1, max_length=300)
    after_step: Identifier
    extraction: TargetSpec
    enum_values: list[str] | None = None
    max_length: int | None = Field(default=None, ge=1, le=4000)

    @model_validator(mode="after")
    def validate_output(self) -> OutputSpec:
        if self.type == ValueType.ENUM and not self.enum_values:
            raise ValueError("enum outputs require enum_values")
        if self.type != ValueType.ENUM and self.enum_values is not None:
            raise ValueError("enum_values is valid only for enum outputs")
        if self.type == ValueType.STRING and self.max_length is None:
            raise ValueError("free-text outputs require max_length")
        return self


class StabilitySpec(StrictModel):
    runs: int = Field(ge=1)
    expected: int = Field(ge=0)
    unexpected: int = Field(ge=0)
    scored_at: datetime

    @model_validator(mode="after")
    def totals_match(self) -> StabilitySpec:
        if self.expected + self.unexpected != self.runs:
            raise ValueError("expected + unexpected must equal runs")
        return self


class Provenance(StrictModel):
    discovery_run_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    timestamp: datetime
    operator: str = Field(min_length=1)
    stability: StabilitySpec | None = None


class Capability(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    capability: CapabilityIdentity
    approval_state: ApprovalState = ApprovalState.DRAFT
    application: ApplicationSpec
    inputs: list[InputSpec] = Field(min_length=1)
    outputs: list[OutputSpec] = Field(default_factory=list)
    interstitials: list[Interstitial] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    provenance: Provenance

    @model_validator(mode="after")
    def validate_contract(self) -> Capability:
        input_names = [item.name for item in self.inputs]
        output_names = [item.name for item in self.outputs]
        step_ids = [step.id for step in self.steps]
        _require_unique(input_names, "input names")
        _require_unique(output_names, "output names")
        _require_unique(step_ids, "step ids")
        _require_unique([item.name for item in self.interstitials], "interstitial names")

        declared_steps = set(step_ids)
        for output in self.outputs:
            if output.after_step not in declared_steps:
                raise ValueError(
                    f"output {output.name} is extracted after undeclared step {output.after_step}"
                )

        declared_inputs = set(input_names)
        for step in self.steps:
            references: list[str] = []
            if step.action.value is not None:
                references.append(step.action.value.from_input)
            if isinstance(step.precondition, RouteCondition):
                references.extend(step.precondition.bindings.values())
            for branch in step.postcondition.any_of:
                if isinstance(branch.condition, RouteCondition):
                    references.extend(branch.condition.bindings.values())
            for reference in references:
                match = INPUT_REF_PATTERN.fullmatch(reference)
                if match is None or match.group(1) not in declared_inputs:
                    raise ValueError(f"step {step.id} references undeclared input {reference}")
        return self

    @classmethod
    def from_yaml(cls, path: str | Path) -> Capability:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(payload)

    def to_yaml(self, path: str | Path) -> None:
        payload = self.model_dump(mode="json", exclude_none=True)
        Path(path).write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _require_unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


def emit_json_schema(path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(Capability.model_json_schema(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination


def main() -> None:
    destination = Path(sys.argv[1] if len(sys.argv) > 1 else "artifacts/schema.json")
    print(emit_json_schema(destination))


if __name__ == "__main__":
    main()
