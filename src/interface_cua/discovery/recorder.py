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

  // Walk up to the nearest interactive ancestor: the model may click the label inside a button.
  const interactive = el.closest('a,button,input,select,textarea,[role],label') || el;
  const row = interactive.closest('tr');
  let anchorText = null;
  if (row) {
    const cells = Array.from(row.querySelectorAll('th,td'));
    const own = cells.find(c => c.contains(interactive));
    const first = cells.find(c => c !== own && c.innerText.trim());
    if (first) anchorText = first.innerText.trim();
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


@dataclass(frozen=True, slots=True)
class RecordedTarget:
    """What was under the click, resolved to something replay can use."""

    target: TargetSpec
    frame: str | None
    accessible_name: str | None
    role: str | None


class Recorder:
    """Resolves click coordinates against a live Playwright page."""

    def __init__(self, page: Any, max_frame_depth: int = 3) -> None:
        self.page = page
        self.max_frame_depth = max_frame_depth

    async def describe_point(self, x: int, y: int) -> RecordedTarget | None:
        """Ask the page what is at (x, y), recursing into same-origin iframes."""

        context: Any = self.page
        frame_name: str | None = None
        local_x, local_y = x, y

        for _ in range(self.max_frame_depth):
            try:
                probe = await context.evaluate(_PROBE_JS, [local_x, local_y])
            except Exception:  # noqa: BLE001 - a cross-origin frame simply cannot be probed
                return None
            if probe is None:
                return None

            if probe["kind"] == "element":
                return _to_target(probe, frame_name)

            # It's an iframe: translate into its coordinate space and descend.
            child = _child_frame(context, probe.get("frameName"))
            if child is None:
                return None
            frame_name = probe.get("frameName") or frame_name
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


def _to_target(probe: dict[str, Any], frame_name: str | None) -> RecordedTarget:
    role = _role_of(probe)
    name = _accessible_name(probe)
    strategies: list[TargetStrategy] = []

    # Ranked most-semantic first. Replay walks this ladder and takes the first *unique* match,
    # so a broad rung that hits several rows is recorded and skipped rather than fatal.
    if role and name:
        strategies.append(
            AccessibilityStrategy(type="accessibility", role=role, name=name, exact=True)
        )
    if name:
        strategies.append(TextStrategy(type="text", value=name, exact=True))
    # The disambiguator: anchor to a unique label in the same table row.
    if role and probe.get("anchorText"):
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
        # Never emit an empty ladder — an artifact that describes nothing is worse than no artifact.
        strategies.append(TextStrategy(type="text", value=probe.get("text") or "", exact=True))

    return RecordedTarget(
        target=TargetSpec(frame=frame_name, strategies=strategies),
        frame=frame_name,
        accessible_name=name,
        role=role,
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
