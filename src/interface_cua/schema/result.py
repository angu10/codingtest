"""Typed terminal results returned by deterministic replay."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class StrictResult(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FailureCategory(StrEnum):
    """Categories the executor can establish *without* interpreting application copy.

    There is deliberately no ``authorization_denied`` here. Telling an entitlement error apart from
    any other refusal screen requires reading the application's own wording, and doing that below
    the model is exactly what the artifact contract exists to prevent. An undeclared refusal
    surfaces as ``target_not_found`` or ``postcondition_failed`` with the observed page text
    attached, and an operator — not the executor — names it.
    """

    TARGET_NOT_FOUND = "target_not_found"
    TARGET_AMBIGUOUS = "target_ambiguous"
    PRECONDITION_FAILED = "precondition_failed"
    POSTCONDITION_FAILED = "postcondition_failed"
    POLICY_DENIED = "policy_denied"
    TIMEOUT = "timeout"
    INVALID_OUTPUT = "invalid_output"
    APPLICATION_ERROR = "application_error"


class ReconciliationState(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    ABSENT = "absent"
    INCONCLUSIVE = "inconclusive"


class SuccessResult(StrictResult):
    status: Literal["success"] = "success"
    outputs: dict[str, Any] = Field(default_factory=dict)
    reconciled: bool = False


class BusinessOutcomeResult(StrictResult):
    status: Literal["business_outcome"] = "business_outcome"
    code: str = Field(min_length=1)
    step: str = Field(min_length=1)
    outputs: dict[str, Any] = Field(default_factory=dict)


class FailureResult(StrictResult):
    status: Literal["failure"] = "failure"
    category: FailureCategory
    retryable: bool
    step: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    observed: str = Field(min_length=1)
    locator_attempts: list[dict[str, Any]] = Field(default_factory=list)
    evidence: str | None = None


class NeedsHumanResult(StrictResult):
    status: Literal["needs_human"] = "needs_human"
    reason: str = Field(min_length=1)
    step: str = Field(min_length=1)
    lease: Literal["HUMAN"] = "HUMAN"


class UnknownSideEffectResult(StrictResult):
    status: Literal["unknown_side_effect"] = "unknown_side_effect"
    step: str = Field(min_length=1)
    reconciliation: ReconciliationState


class ValidationCheck(StrEnum):
    """Which gate refused to let the run start. Never silent execution — see invariant 3."""

    APPROVAL = "approval"
    APPLICATION = "application"
    ENTRY_ROUTE = "entry_route"
    ENTRY_LANDMARKS = "entry_landmarks"


class ValidationRequiredResult(StrictResult):
    status: Literal["validation_required"] = "validation_required"
    check: ValidationCheck
    reason: str = Field(min_length=1)
    expected: dict[str, Any]
    observed: dict[str, Any]


ReplayResult = Annotated[
    SuccessResult
    | BusinessOutcomeResult
    | FailureResult
    | NeedsHumanResult
    | UnknownSideEffectResult
    | ValidationRequiredResult,
    Field(discriminator="status"),
]

ReplayResultAdapter: TypeAdapter[ReplayResult] = TypeAdapter(ReplayResult)

