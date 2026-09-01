"""Deterministic semantic strategy ladder with fail-closed uniqueness."""

from __future__ import annotations

from dataclasses import dataclass

from interface_cua.replay.polling import poll_until
from interface_cua.schema.artifact import TargetSpec
from interface_cua.surface.base import SurfaceAdapter, SurfaceElement


@dataclass(frozen=True, slots=True)
class LocatorAttempt:
    strategy: dict[str, object]
    matches: int
    rejected_because: str


class TargetResolutionError(RuntimeError):
    def __init__(self, message: str, attempts: list[LocatorAttempt]) -> None:
        super().__init__(message)
        self.attempts = attempts


class TargetNotFound(TargetResolutionError):
    pass


class TargetAmbiguous(TargetResolutionError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    element: SurfaceElement
    attempts: tuple[LocatorAttempt, ...]


class TargetResolver:
    """Resolves a `TargetSpec` down its strategy ladder to exactly one element.

    Absence is treated as a timing question and waited out; ambiguity is not. Two matches means the
    artifact does not describe the screen precisely enough, and no amount of waiting fixes that —
    so ambiguity fails immediately rather than racing (invariant 2).
    """

    def __init__(self, surface: SurfaceAdapter, timeout_ms: int = 5_000) -> None:
        self.surface = surface
        self.timeout_ms = timeout_ms

    async def resolve(self, target: TargetSpec, *, timeout_ms: int | None = None) -> ResolvedTarget:
        async def probe() -> ResolvedTarget | None:
            try:
                return await self.resolve_once(target)
            except TargetNotFound:
                return None

        resolved = await poll_until(
            probe, self.timeout_ms if timeout_ms is None else timeout_ms
        )
        if resolved is not None:
            return resolved
        return await self.resolve_once(target)

    async def resolve_once(self, target: TargetSpec) -> ResolvedTarget:
        """Walk the ladder and return the first strategy that matches exactly one element.

        An ambiguous rung is recorded and skipped rather than fatal, because that is precisely what
        the narrower rungs below it are for: `Open Sub-Account` matches every account row, while
        "the `Open Sub-Account` in the Savings Account row" matches one. Skipping is still
        fail-closed — no rung ever picks among its own matches, and if none is unique the whole
        resolution fails (invariant 2).
        """

        attempts: list[LocatorAttempt] = []
        ambiguous = False
        for strategy in target.strategies:
            try:
                matches = await self.surface.find(strategy, target.frame)
            except LookupError as exc:
                attempts.append(
                    LocatorAttempt(strategy.model_dump(mode="json"), 0, str(exc))
                )
                continue
            count = len(matches)
            if count == 1:
                attempts.append(
                    LocatorAttempt(strategy.model_dump(mode="json"), 1, "accepted:unique")
                )
                return ResolvedTarget(matches[0], tuple(attempts))
            if count > 1:
                ambiguous = True
                attempts.append(
                    LocatorAttempt(strategy.model_dump(mode="json"), count, "rejected:ambiguous")
                )
                continue
            attempts.append(
                LocatorAttempt(strategy.model_dump(mode="json"), 0, "rejected:not_found")
            )
        if ambiguous:
            # Something was on screen; the artifact just does not describe it precisely enough.
            # Reported separately from "not found" because the fix is a better locator, not a wait.
            raise TargetAmbiguous(
                "no strategy matched exactly one target; refusing to choose", attempts
            )
        raise TargetNotFound("no target strategy produced a unique match", attempts)

