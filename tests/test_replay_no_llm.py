from __future__ import annotations

from datetime import UTC, datetime

import pytest

from interface_cua.policy.engine import PolicyConfig, PolicyEngine
from interface_cua.replay.executor import ReplayExecutor
from interface_cua.schema.artifact import (
    AccessibilityStrategy,
    ActionSpec,
    ApplicationFingerprint,
    ApplicationSpec,
    ApprovalState,
    Capability,
    CapabilityIdentity,
    InputSpec,
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
    TextCondition,
    ValueType,
)
from interface_cua.schema.result import FailureCategory


class FakeElement:
    description = "button: Continue"

    def __init__(self, surface: FakeSurface) -> None:
        self.surface = surface

    async def click(self) -> None:
        self.surface.state = "done"

    async def fill(self, value: str) -> None: ...

    async def select(self, value: str) -> None: ...

    async def text(self) -> str:
        return "Continue"

    async def is_visible(self) -> bool:
        return True

    async def is_enabled(self) -> bool:
        return True


class FakeSurface:
    current_url = "https://synthetic.test/start"

    def __init__(self) -> None:
        self.state = "ready"
        self.element = FakeElement(self)

    async def find(self, strategy, frame):
        if getattr(strategy, "name", None) == "Missing":
            return []
        return [self.element]

    async def page_text(self) -> str:
        return self.state

    async def navigate(self, url: str) -> None: ...

    async def keypress(self, key: str) -> None: ...

    async def wait_until_settled(self, timeout_ms: int) -> None: ...

    async def screenshot(self) -> bytes:
        return b""

    async def dom_snapshot(self) -> str:
        return "<p>synthetic</p>"


def capability() -> Capability:
    target = TargetSpec(
        strategies=[
            AccessibilityStrategy(type="accessibility", role="button", name="Continue")
        ]
    )
    return Capability(
        capability=CapabilityIdentity(
            id="offline-replay",
            version="1.0.0",
            description="Minimal executor proof with no model dependency.",
        ),
        approval_state=ApprovalState.APPROVED,
        application=ApplicationSpec(
            family="synthetic-app",
            supported_versions=["v1"],
            fingerprint=ApplicationFingerprint(
                route_patterns=["/start"], entry_landmarks=["ready"]
            ),
        ),
        inputs=[
            InputSpec(
                name="goal",
                type=ValueType.STRING,
                description="Synthetic required input.",
                max_length=20,
            )
        ],
        steps=[
            Step(
                id="continue",
                precondition=PageCondition(
                    type="page", name="ready-page", landmark=target
                ),
                action=ActionSpec(type="click"),
                target=target,
                postcondition=Postcondition(
                    any_of=[
                        PostconditionBranch(
                            name="done", condition=TextCondition(type="text", value="done")
                        )
                    ]
                ),
                retry=RetrySpec(max_attempts=1, safe=True),
                risk=RiskSpec(level=RiskLevel.READ),
            )
        ],
        provenance=Provenance(
            discovery_run_id="offline-test",
            model_id="none",
            timestamp=datetime.now(UTC),
            operator="pytest",
        ),
    )


def _executor() -> ReplayExecutor:
    return ReplayExecutor(
        FakeSurface(),
        PolicyEngine(PolicyConfig(allowed_origins=frozenset({"https://synthetic.test"}))),
        application_family="synthetic-app",
        application_version="v1",
    )


@pytest.mark.asyncio
async def test_replay_succeeds_with_anthropic_key_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = await _executor().execute(capability(), {"goal": "continue"})
    assert result.status == "success"


@pytest.mark.asyncio
async def test_unextractable_declared_output_is_a_failure_not_a_silent_omission() -> None:
    """A capability that promises an output must not report success without it."""

    artifact = capability()
    artifact.outputs = [
        OutputSpec(
            name="balance",
            type=ValueType.DECIMAL,
            description="A value the page will not yield.",
            after_step="continue",
            extraction=TargetSpec(
                strategies=[
                    AccessibilityStrategy(type="accessibility", role="cell", name="Missing")
                ]
            ),
        )
    ]
    result = await _executor().execute(artifact, {"goal": "continue"})
    assert result.status == "failure"
    assert result.category == FailureCategory.INVALID_OUTPUT
    assert "balance" in result.expected


@pytest.mark.asyncio
async def test_output_that_fails_type_coercion_is_a_failure() -> None:
    """Typed outputs are the boundary that stops page text flowing into a caller's model context."""

    artifact = capability()
    artifact.outputs = [
        OutputSpec(
            name="balance",
            type=ValueType.DECIMAL,
            description="The fake element yields the word 'Continue', not a decimal.",
            after_step="continue",
            extraction=TargetSpec(
                strategies=[
                    AccessibilityStrategy(type="accessibility", role="cell", name="Balance")
                ]
            ),
        )
    ]
    result = await _executor().execute(artifact, {"goal": "continue"})
    assert result.status == "failure"
    assert result.category == FailureCategory.INVALID_OUTPUT

