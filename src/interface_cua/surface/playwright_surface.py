"""Lease-enforced Playwright implementation of the browser surface."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Locator, Page

from interface_cua.handoff.lease import Controller, SessionLease
from interface_cua.schema.artifact import (
    AccessibilityStrategy,
    RelativeStrategy,
    TargetStrategy,
    TextStrategy,
)

VIEWPORT = (1280, 800)


@dataclass(slots=True)
class PlaywrightElement:
    locator: Locator
    lease: SessionLease
    caller: Controller
    _description: str

    @property
    def description(self) -> str:
        return self._description

    async def click(self) -> None:
        self.lease.assert_mutation_allowed(self.caller)
        await self.locator.click()

    async def fill(self, value: str) -> None:
        self.lease.assert_mutation_allowed(self.caller)
        await self.locator.fill(value)

    async def select(self, value: str) -> None:
        self.lease.assert_mutation_allowed(self.caller)
        await self.locator.select_option(value=value)

    async def text(self) -> str:
        return (await self.locator.inner_text()).strip()

    async def is_visible(self) -> bool:
        return await self.locator.is_visible()

    async def is_enabled(self) -> bool:
        return await self.locator.is_enabled()


class PlaywrightSurface:
    def __init__(
        self,
        page: Page,
        lease: SessionLease,
        caller: Controller = Controller.AUTOMATION,
    ) -> None:
        self.page = page
        self.lease = lease
        self.caller = caller

    @property
    def current_url(self) -> str:
        return self.page.url

    async def find(self, strategy: TargetStrategy, frame: str | None) -> list[PlaywrightElement]:
        root = self._root(frame)
        locator = self._locator(root, strategy)
        count = await locator.count()
        return [
            PlaywrightElement(
                locator.nth(index),
                self.lease,
                self.caller,
                f"{strategy.type}:{strategy.model_dump(exclude={'type'})}",
            )
            for index in range(count)
        ]

    async def page_text(self) -> str:
        texts: list[str] = []
        for frame in self.page.frames:
            try:
                texts.append(await frame.locator("body").inner_text())
            except PlaywrightError:
                continue
        return "\n".join(texts)

    async def navigate(self, url: str) -> None:
        self.lease.assert_mutation_allowed(self.caller)
        await self.page.goto(url, wait_until="domcontentloaded")

    async def keypress(self, key: str) -> None:
        self.lease.assert_mutation_allowed(self.caller)
        await self.page.keyboard.press(key)

    async def wait_until_settled(self, timeout_ms: int) -> None:
        # Deliberately not `networkidle`: that waits on a traffic heuristic rather than on the
        # state the step actually needs. Postconditions do the real waiting, against conditions
        # the artifact declared.
        await self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        # Child frames too. On this app the account pane *is* the content, so a main frame that
        # has loaded while its iframe has not is not settled in any useful sense — and a
        # page-level screenshot blocks on every frame, so skipping this hangs the capture.
        for frame in self.page.frames:
            if frame is self.page.main_frame:
                continue
            try:
                await frame.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except PlaywrightError:
                continue  # detached mid-wait; the postcondition is what actually decides

    async def screenshot(self, timeout_ms: int = 10_000) -> bytes:
        # Bounded: a page that never settles must fail the step, not stall the whole run.
        payload = await self.page.screenshot(type="png", timeout=timeout_ms)
        width, height = struct.unpack(">II", payload[16:24])
        if (width, height) != VIEWPORT:
            raise RuntimeError(
                f"screenshot dimensions {(width, height)} do not match viewport {VIEWPORT}"
            )
        return payload

    async def dom_snapshot(self) -> str:
        return await self.page.content()

    # Tracing is deliberately not on `SurfaceAdapter`: it is a Playwright-specific richer signal,
    # and a future desktop surface would have its own equivalent. `EvidenceWriter` probes for it.
    async def start_trace(self) -> None:
        await self.page.context.tracing.start(screenshots=True, snapshots=True, sources=False)

    async def save_trace(self, path: Path) -> None:
        await self.page.context.tracing.stop(path=str(path))

    def _root(self, frame_name: str | None) -> Page | Frame:
        if frame_name is None:
            return self.page
        frame = self.page.frame(name=frame_name)
        if frame is None:
            raise LookupError(f"frame not found: {frame_name}")
        return frame

    @staticmethod
    def _locator(root: Page | Frame, strategy: TargetStrategy) -> Locator:
        if isinstance(strategy, AccessibilityStrategy):
            return root.get_by_role(
                strategy.role,  # type: ignore[arg-type]
                name=strategy.name,
                exact=strategy.exact,
            )
        if isinstance(strategy, TextStrategy):
            return root.get_by_text(strategy.value, exact=strategy.exact)
        if isinstance(strategy, RelativeStrategy):
            anchor = root.get_by_text(strategy.anchor, exact=True)
            if strategy.relation == "following_cell":
                return anchor.locator("xpath=following-sibling::td[1]")
            row = anchor.locator("xpath=ancestor::tr[1]")
            options: dict[str, Any] = {}
            if strategy.name is not None:
                options = {"name": strategy.name, "exact": True}
            return row.get_by_role(strategy.role, **options)  # type: ignore[arg-type]
        raise TypeError(f"unsupported strategy: {type(strategy).__name__}")
