"""The recorder turns a click coordinate into a semantic target, against the real hostile DOM."""

from __future__ import annotations

import pytest
from playwright.async_api import async_playwright

from interface_cua.discovery.recorder import Recorder

pytestmark = pytest.mark.asyncio


async def _record_click_on(demo_server: str, path: str, selector: str, frame_name: str | None = None):
    """Click-target recording: find an element, take its centre, ask the recorder what's there."""

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.goto(f"{demo_server}{path}")
        await page.wait_for_load_state("networkidle")

        scope = page.frame(name=frame_name) if frame_name else page
        assert scope is not None, f"frame {frame_name} never attached"
        # Playwright reports bounding boxes relative to the *main frame* viewport, even for
        # elements inside an iframe — so this is already the page coordinate a model would click.
        box = await scope.locator(selector).first.bounding_box()
        assert box is not None, f"{selector} has no layout box"
        point = (int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
        try:
            return await Recorder(page).describe_point(*point)
        finally:
            await browser.close()


async def test_recorder_names_a_top_level_control(demo_server: str) -> None:
    recorded = await _record_click_on(demo_server, "/", "button.btn")
    assert recorded is not None
    assert recorded.role == "button"
    assert recorded.accessible_name == "Search"
    assert recorded.frame is None


async def test_recorder_names_a_textbox_from_its_wrapping_label(demo_server: str) -> None:
    """The input has no id, no name attribute the model can see — only a wrapping <label>."""

    recorded = await _record_click_on(demo_server, "/", 'input[name="member_id"]')
    assert recorded is not None
    assert recorded.role == "textbox"
    assert recorded.accessible_name == "Member ID"


async def test_recorder_pierces_the_iframe_and_records_the_frame_name(demo_server: str) -> None:
    """`elementFromPoint` stops at the iframe boundary; the walk has to be explicit."""

    recorded = await _record_click_on(
        demo_server, "/member/58431", "a.btn", frame_name="account-frame"
    )
    assert recorded is not None
    assert recorded.frame == "account-frame"
    assert recorded.role == "link"
    assert recorded.accessible_name == "Open Sub-Account"


async def test_recorder_emits_a_row_anchor_when_the_name_is_ambiguous(demo_server: str) -> None:
    """58431 has two rows offering an identical link — the anchor is what separates them."""

    recorded = await _record_click_on(
        demo_server, "/member/58431", "a.btn", frame_name="account-frame"
    )
    assert recorded is not None
    kinds = [strategy.type for strategy in recorded.target.strategies]
    assert "relative" in kinds, kinds

    anchored = next(s for s in recorded.target.strategies if s.type == "relative")
    assert anchored.anchor == "Savings Account"
    assert anchored.role == "link"

    # And the ladder is ordered most-semantic first, so replay tries the plain name before it
    # falls through to the structural rung.
    assert kinds[0] == "accessibility"


async def test_recorded_targets_never_contain_coordinates(demo_server: str) -> None:
    """The artifact must never contain a coordinate — that is the whole point of the recorder."""

    recorded = await _record_click_on(
        demo_server, "/member/58431", "a.btn", frame_name="account-frame"
    )
    assert recorded is not None
    serialised = recorded.target.model_dump_json()
    for banned in ("\"x\"", "\"y\"", "coordinate", "clientX", "pixel"):
        assert banned not in serialised, serialised
