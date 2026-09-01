from __future__ import annotations

from dataclasses import dataclass

import pytest

from interface_cua.replay.targeting import TargetAmbiguous, TargetNotFound, TargetResolver
from interface_cua.schema.artifact import AccessibilityStrategy, TargetSpec


@dataclass
class FakeElement:
    description: str

    async def click(self) -> None: ...

    async def fill(self, value: str) -> None: ...

    async def select(self, value: str) -> None: ...

    async def text(self) -> str:
        return self.description

    async def is_visible(self) -> bool:
        return True

    async def is_enabled(self) -> bool:
        return True


class FakeSurface:
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts

    async def find(self, strategy, frame):
        count = self.counts.get(strategy.name, 0)
        return [FakeElement(f"match-{index}") for index in range(count)]


def spec(*names: str) -> TargetSpec:
    return TargetSpec(
        strategies=[
            AccessibilityStrategy(type="accessibility", role="button", name=name)
            for name in names
        ]
    )


@pytest.mark.asyncio
async def test_strategy_ladder_uses_first_unique_match() -> None:
    resolved = await TargetResolver(FakeSurface({"primary": 0, "fallback": 1})).resolve(
        spec("primary", "fallback")
    )
    assert resolved.element.description == "match-0"
    assert [attempt.matches for attempt in resolved.attempts] == [0, 1]


@pytest.mark.asyncio
async def test_ambiguous_rung_is_skipped_so_a_narrower_one_can_disambiguate() -> None:
    """`Open Sub-Account` matches every account row; the row-anchored rung below it matches one."""

    resolved = await TargetResolver(FakeSurface({"primary": 2, "fallback": 1})).resolve(
        spec("primary", "fallback")
    )
    assert resolved.element.description == "match-0"
    assert [attempt.rejected_because for attempt in resolved.attempts] == [
        "rejected:ambiguous",
        "accepted:unique",
    ]


@pytest.mark.asyncio
async def test_ambiguity_with_no_unique_rung_still_refuses_to_choose() -> None:
    """Skipping a rung is not the same as picking one of its matches (invariant 2)."""

    with pytest.raises(TargetAmbiguous) as captured:
        await TargetResolver(FakeSurface({"primary": 2, "fallback": 3})).resolve(
            spec("primary", "fallback")
        )
    assert [attempt.matches for attempt in captured.value.attempts] == [2, 3]


@pytest.mark.asyncio
async def test_ambiguity_is_reported_separately_from_absence() -> None:
    """The fix for an ambiguous target is a better locator; the fix for absence is a wait."""

    with pytest.raises(TargetAmbiguous):
        await TargetResolver(FakeSurface({"primary": 2, "fallback": 0}), timeout_ms=0).resolve(
            spec("primary", "fallback")
        )


@pytest.mark.asyncio
async def test_no_match_reports_every_attempt() -> None:
    with pytest.raises(TargetNotFound) as captured:
        await TargetResolver(FakeSurface({})).resolve(spec("primary", "fallback"))
    assert len(captured.value.attempts) == 2

