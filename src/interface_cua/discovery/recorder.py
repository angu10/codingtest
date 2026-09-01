"""Coordinate → `TargetSpec`. The piece that makes replay semantic.

Discovery is visual: the model looks at a screenshot and clicks a pixel. Replay must not be — a
stored coordinate breaks on the next layout change and says nothing a reviewer can audit. So after
every click we ask the *page* what was actually under that point and synthesise a ranked ladder of
semantic strategies from it.

`document.elementFromPoint` does not pierce iframes, so the walk is explicit and recursive, with
coordinates translated into each frame's own space on the way down.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from interface_cua.schema.artifact import (
    AccessibilityStrategy,
    RelativeStrategy,
    TargetSpec,
    TargetStrategy,
    TextStrategy,
)

#: Runs inside a frame. Returns what is under (x, y) *in that frame's coordinate space*, plus the
#: geometry of any iframe under the point so the caller can recurse into it.
_PROBE_JS = """
([x, y]) => {
  const el = document.elementFromPoint(x, y);
  if (!el) return null;

  const frame = el.tagName === 'IFRAME' ? el : null;
  if (frame) {
    const r = frame.getBoundingClientRect();
    return {
      kind: 'frame',
      frameName: frame.getAttribute('name'),
      offsetX: r.left + frame.clientLeft,
      offsetY: r.top + frame.clientTop,
    };
  }

  const SELECTOR = 'a,button,input,select,textarea,[role],label';

  // Walk up first: the model may have clicked the text *inside* a button.
  let interactive = el.closest(SELECTOR);

  // Still nothing? Then the point landed on a container. A vision model estimates coordinates
  // from a screenshot and is routinely tens of pixels out on dense UIs — accurate enough to mean
  // one control, not accurate enough to land inside it. Snap to the nearest control within a
  // small radius rather than actuating a <div> and reporting that nothing happened.
  let snapped = false;
  if (!interactive) {
    const RADIUS = 60;
    let best = null, bestDistance = Infinity;
    for (const candidate of document.querySelectorAll(SELECTOR)) {
      const r = candidate.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      const dx = Math.max(r.left - x, 0, x - r.right);
      const dy = Math.max(r.top - y, 0, y - r.bottom);
      const distance = Math.hypot(dx, dy);
      if (distance < bestDistance) { best = candidate; bestDistance = distance; }
    }
    if (best && bestDistance <= RADIUS) { interactive = best; snapped = true; }
  }
  interactive = interactive || el;

  const rect = interactive.getBoundingClientRect();
  const row = interactive.closest('tr');
  let anchorText = null;
  let previousCellText = null;
  if (row) {
    const cells = Array.from(row.querySelectorAll('th,td'));
    const own = cells.find(c => c.contains(interactive));
    const first = cells.find(c => c !== own && c.innerText.trim());
    if (first) anchorText = first.innerText.trim();
    // The cell immediately before this one. For a *value* cell that label is the only stable
    // handle: the value itself changes every run, its neighbour does not.
    if (own) {
      const i = cells.indexOf(own);
      if (i > 0) previousCellText = cells[i - 1].innerText.trim() || null;
    }
  }

  return {
    kind: 'element',
    tag: interactive.tagName.toLowerCase(),
    type: interactive.getAttribute('type'),
    role: interactive.getAttribute('role'),
    text: (interactive.innerText || interactive.value || '').trim().slice(0, 120),
    ariaLabel: interactive.getAttribute('aria-label'),
    labelText: (() => {
      const l = interactive.closest('label');
      if (!l) return null;
      // The label's own text, minus the control's, is the accessible name.
      return l.innerText.replace(interactive.innerText || '', '').trim().slice(0, 120);
    })(),
    anchorText,
    previousCellText,
    snapped,
    // Where the control actually is, so the caller can act on it rather than on the estimate.
    centreX: rect.left + rect.width / 2,
    centreY: rect.top + rect.height / 2,
  };
}
"""

#: How an HTML tag maps to the ARIA role Playwright's `get_by_role` expects.
_TAG_ROLES = {
    "a": "link",
    "button": "button",
    "select": "combobox",
    "textarea": "textbox",
}

_INPUT_TYPE_ROLES = {
    "submit": "button",
    "button": "button",
    "checkbox": "checkbox",
    "radio": "radio",
}


class UnanchorableValue(RuntimeError):
    """The only handle on this element is a value that will change next run."""


def _is_volatile(name: str | None, avoid_text: str | None) -> bool:
    """True when the element's own text is the value we are trying to extract."""

    if not name or not avoid_text:
        return False

    def normalise(text: str) -> str:
        return text.replace("$", "").replace(",", "").strip()

    left, right = normalise(avoid_text), normalise(name)
    return bool(left) and bool(right) and (left in right or right in left)


@dataclass(frozen=True, slots=True)
class RecordedTarget:
    """What was under the click, resolved to something replay can use."""

    target: TargetSpec
    frame: str | None
    accessible_name: str | None
    role: str | None
    #: Centre of the resolved control, in *page* coordinates. Differs from the requested point
    #: when the estimate was snapped to a nearby control.
    point: tuple[int, int]
    #: True when the requested point was not inside any control and we snapped to the nearest.
    snapped: bool


class Recorder:
    """Resolves click coordinates against a live Playwright page."""

    def __init__(self, page: Any, max_frame_depth: int = 3) -> None:
        self.page = page
        self.max_frame_depth = max_frame_depth

    async def describe_point(
        self, x: int, y: int, *, avoid_text: str | None = None
    ) -> RecordedTarget | None:
        """Ask the page what is at (x, y), recursing into same-origin iframes.

        `avoid_text` is set when recording an *output*: the thing under the point is a value that
        will differ on the next run, so any strategy that matches on it is worse than useless. It
        forces the ladder to anchor structurally instead.
        """

        context: Any = self.page
        frame_name: str | None = None
        local_x, local_y = x, y
        offset_x = offset_y = 0

        for _ in range(self.max_frame_depth):
            try:
                probe = await context.evaluate(_PROBE_JS, [local_x, local_y])
            except Exception:  # noqa: BLE001 - a cross-origin frame simply cannot be probed
                return None
            if probe is None:
                return None

            if probe["kind"] == "element":
                try:
                    return _to_target(probe, frame_name, avoid_text, (offset_x, offset_y))
                except UnanchorableValue:
                    return None

            # It's an iframe: translate into its coordinate space and descend.
            child = _child_frame(context, probe.get("frameName"))
            if child is None:
                return None
            frame_name = probe.get("frameName") or frame_name
            offset_x += int(probe["offsetX"])
            offset_y += int(probe["offsetY"])
            local_x -= int(probe["offsetX"])
            local_y -= int(probe["offsetY"])
            context = child
        return None


def _child_frame(context: Any, name: str | None) -> Any | None:
    frames = getattr(context, "child_frames", None) or getattr(context, "frames", None) or []
    if name:
        for frame in frames:
            if frame.name == name:
                return frame
    return frames[0] if frames else None


def _to_target(
    probe: dict[str, Any],
    frame_name: str | None,
    avoid_text: str | None = None,
    frame_offset: tuple[int, int] = (0, 0),
) -> RecordedTarget:
    role = _role_of(probe)
    name = _accessible_name(probe)
    volatile = _is_volatile(name, avoid_text)
    strategies: list[TargetStrategy] = []

    # A value cell is addressed by its neighbouring label, never by its own contents.
    if volatile and probe.get("previousCellText"):
        strategies.append(
            RelativeStrategy(
                type="relative",
                anchor=probe["previousCellText"],
                role="cell",
                relation="following_cell",
            )
        )

    # Ranked most-semantic first. Replay walks this ladder and takes the first *unique* match,
    # so a broad rung that hits several rows is recorded and skipped rather than fatal.
    if role and name and not volatile:
        strategies.append(
            AccessibilityStrategy(type="accessibility", role=role, name=name, exact=True)
        )
    if name and not volatile:
        strategies.append(TextStrategy(type="text", value=name, exact=True))
    # The disambiguator: anchor to a unique label in the same table row.
    if role and probe.get("anchorText") and not volatile:
        strategies.append(
            RelativeStrategy(
                type="relative",
                anchor=probe["anchorText"],
                role=role,
                name=name,
                relation="same_row",
            )
        )

    if not strategies:
        # Never emit an empty ladder — an artifact that describes nothing is worse than no
        # artifact. If the only handle left would be the volatile value, refuse it entirely and
        # let the caller record the output as unanchored.
        if volatile:
            raise UnanchorableValue(
                f"nothing but the value itself identifies {(name or '')[:40]!r}"
            )
        strategies.append(TextStrategy(type="text", value=probe.get("text") or "", exact=True))

    return RecordedTarget(
        target=TargetSpec(frame=frame_name, strategies=strategies),
        frame=frame_name,
        accessible_name=name,
        role=role,
        point=(
            int(probe["centreX"]) + frame_offset[0],
            int(probe["centreY"]) + frame_offset[1],
        ),
        snapped=bool(probe.get("snapped")),
    )


def _role_of(probe: dict[str, Any]) -> str | None:
    if probe.get("role"):
        return str(probe["role"])
    tag = probe.get("tag")
    if tag == "input":
        return _INPUT_TYPE_ROLES.get(str(probe.get("type") or "text"), "textbox")
    return _TAG_ROLES.get(str(tag))


def _accessible_name(probe: dict[str, Any]) -> str | None:
    for key in ("ariaLabel", "labelText", "text"):
        value = probe.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return None
