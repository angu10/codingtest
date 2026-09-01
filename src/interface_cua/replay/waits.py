"""Explicit state checks used as replay checkpoints; see `polling` for the waiting discipline."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from interface_cua.replay.polling import poll_until
from interface_cua.replay.targeting import TargetAmbiguous, TargetNotFound, TargetResolver
from interface_cua.schema.artifact import (
    Condition,
    PageCondition,
    RouteCondition,
    TargetStateCondition,
    TextCondition,
)
from interface_cua.surface.base import SurfaceAdapter


class ConditionChecker:
    def __init__(self, surface: SurfaceAdapter, resolver: TargetResolver) -> None:
        self.surface = surface
        self.resolver = resolver

    async def wait_for(
        self, condition: Condition, inputs: dict[str, object], timeout_ms: int
    ) -> bool:
        async def probe() -> bool | None:
            return True if await self.matches(condition, inputs) else None

        return await poll_until(probe, timeout_ms) is not None

    async def matches(self, condition: Condition, inputs: dict[str, object]) -> bool:
        if isinstance(condition, PageCondition):
            return await self._target_state(condition.landmark, "visible")
        if isinstance(condition, TargetStateCondition):
            return await self._target_state(condition.target, condition.state)
        if isinstance(condition, TextCondition):
            expected = condition.value
            if expected is None and condition.from_input is not None:
                name = condition.from_input.removeprefix("${inputs.").removesuffix("}")
                expected = str(inputs[name])
            return bool(expected) and expected in await self.surface.page_text()
        if isinstance(condition, RouteCondition):
            return _route_matches(condition, self.surface.current_url, inputs)
        raise TypeError(f"unsupported condition: {type(condition).__name__}")

    async def _target_state(self, target: object, state: str) -> bool:
        # A single instantaneous look. Waiting happens once, in `wait_for`, around the whole
        # condition — never per strategy, which would multiply the deadline by the ladder length.
        try:
            resolved = await self.resolver.resolve_once(target)  # type: ignore[arg-type]
        except TargetNotFound:
            return state == "hidden"
        except TargetAmbiguous:
            raise
        visible = await resolved.element.is_visible()
        if state == "visible":
            return visible
        if state == "hidden":
            return not visible
        enabled = await resolved.element.is_enabled()
        return enabled if state == "enabled" else not enabled


def route_match(pattern: str, url: str) -> re.Match[str] | None:
    """Match a `/member/:id`-style route template against a URL's path."""

    escaped = re.escape(pattern)
    for name in re.findall(r":([a-zA-Z_][a-zA-Z0-9_]*)", pattern):
        escaped = escaped.replace(rf":{name}", rf"(?P<{name}>[^/]+)")
    return re.fullmatch(escaped, urlparse(url).path)


def _route_matches(
    condition: RouteCondition, current_url: str, inputs: dict[str, object]
) -> bool:
    match = route_match(condition.pattern, current_url)
    if match is None:
        return False
    for route_name, reference in condition.bindings.items():
        input_name = reference.removeprefix("${inputs.").removesuffix("}")
        if match.groupdict().get(route_name) != str(inputs[input_name]):
            return False
    return True

