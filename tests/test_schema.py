from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from interface_cua.schema.artifact import (
    AccessibilityStrategy,
    ActionSpec,
    ApplicationFingerprint,
    ApplicationSpec,
    ApprovalState,
    Capability,
    CapabilityIdentity,
    InputSpec,
    InputValue,
    OutputSpec,
    PageCondition,
    Postcondition,
    PostconditionBranch,
    Provenance,
    RetrySpec,
    RiskLevel,
    RiskSpec,
    Step,
    TargetSpec,
    ValueType,
    emit_json_schema,
)
from interface_cua.schema.result import ReplayResultAdapter, ValidationCheck


def target(name: str) -> TargetSpec:
    return TargetSpec(
        frame="account-frame",
        strategies=[AccessibilityStrategy(type="accessibility", role="textbox", name=name)],
    )


def capability() -> Capability:
    return Capability(
        capability=CapabilityIdentity(
            id="open-sub-account-review",
            version="1.0.0",
            description="Reach the review page without creating an account.",
        ),
        approval_state=ApprovalState.DRAFT,
        application=ApplicationSpec(
            family="meridian-cu",
            supported_versions=["demo-v1"],
            fingerprint=ApplicationFingerprint(
                route_patterns=["/", "/member/:id"],
                entry_landmarks=["Member Search"],
            ),
        ),
        inputs=[
            InputSpec(
                name="member_id",
                type=ValueType.STRING,
                description="Synthetic member reference",
                sensitive=True,
                min_length=5,
                max_length=5,
                pattern=r"^\d{5}$",
            )
        ],
        outputs=[
            OutputSpec(
                name="current_balance",
                type=ValueType.DECIMAL,
                description="Current savings balance",
                after_step="fill-member-id",
                extraction=target("Current balance"),
            )
        ],
        steps=[
            Step(
                id="fill-member-id",
                precondition=PageCondition(
                    type="page", name="member-search", landmark=target("Member ID")
                ),
                action=ActionSpec(
                    type="fill", value=InputValue(from_input="${inputs.member_id}")
                ),
                target=target("Member ID"),
                postcondition=Postcondition(
                    any_of=[
                        PostconditionBranch(
                            name="member-found",
                            condition=PageCondition(
                                type="page", name="member-detail", landmark=target("Member ID")
                            ),
                        ),
                        PostconditionBranch(
                            name="not-found",
                            condition=PageCondition(
                                type="page", name="member-not-found", landmark=target("Member ID")
                            ),
                            business_outcome="MEMBER_NOT_FOUND",
                        ),
                    ]
                ),
                retry=RetrySpec(max_attempts=2, safe=True),
                risk=RiskSpec(level=RiskLevel.READ),
            )
        ],
        provenance=Provenance(
            discovery_run_id="disc_synthetic",
            model_id="verified-at-discovery-time",
            timestamp=datetime.now(UTC),
            operator="test-operator",
        ),
    )


def test_artifact_yaml_round_trip_and_json_schema(tmp_path) -> None:
    original = capability()
    yaml_path = tmp_path / "capability.yaml"
    schema_path = tmp_path / "schema.json"
    original.to_yaml(yaml_path)
    restored = Capability.from_yaml(yaml_path)
    assert restored == original

    emitted = emit_json_schema(schema_path)
    assert emitted == schema_path
    assert '"discriminator"' in schema_path.read_text(encoding="utf-8")
    assert '"must_be_unique"' in schema_path.read_text(encoding="utf-8")


def test_undeclared_input_reference_is_rejected() -> None:
    payload = capability().model_dump(mode="json")
    payload["steps"][0]["action"]["value"]["from_input"] = "${inputs.account_number}"
    with pytest.raises(ValidationError, match="undeclared input"):
        Capability.model_validate(payload)


def test_consequential_write_cannot_be_retried() -> None:
    payload = capability().model_dump(mode="json")
    payload["steps"][0]["risk"] = {
        "level": "consequential_write",
        "requires_confirmation": True,
    }
    payload["steps"][0]["retry"] = {"max_attempts": 2, "safe": True}
    with pytest.raises(ValidationError, match="single-attempt"):
        Capability.model_validate(payload)


def test_validation_required_is_a_typed_replay_result() -> None:
    result = ReplayResultAdapter.validate_python(
        {
            "status": "validation_required",
            "check": "entry_route",
            "reason": "session is not on a route this capability was authored against",
            "expected": {"route_patterns": ["/member/:id"]},
            "observed": {"url": "http://localhost:8000/customer/1"},
        }
    )
    assert result.status == "validation_required"
    assert result.check == ValidationCheck.ENTRY_ROUTE
