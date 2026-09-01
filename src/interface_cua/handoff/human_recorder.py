"""Capture what a human did while they held the lease.

Two properties matter more than completeness here.

**Values are masked inside the page.** The listener redacts before anything crosses back into
Python, so a typed member id becomes `***8431` at the point of capture rather than on the way to
disk. A password field is recorded as `[redacted]` with no length. There is no moment where the
raw value exists in our process, which is a stronger claim than "we redact before writing".

**Actions are recorded semantically.** Role and accessible name, never a CSS selector — the same
vocabulary the artifact uses, so a human's actions and an artifact's steps are comparable.

The listener is installed with `add_init_script` so it survives navigations; the human will move
between pages and we do not get to re-inject on each one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

#: Runs in the page. Mirrors `Redactor`'s rules deliberately — the Python side is the auditable
#: control, this is the same policy applied early so raw values never leave the browser.
_LISTENER_JS = """
(config) => {
  if (window.__cuaHumanRecorderInstalled) return;
  window.__cuaHumanRecorderInstalled = true;

  const mask = (name, value) => {
    if (value == null || value === '') return '';
    const lowered = (name || '').toLowerCase();
    if (lowered.includes('password') || lowered.includes('secret')) return '[redacted]';
    const text = String(value);
    // Anything the operator was told is sensitive, plus anything that looks like an identifier,
    // is reduced to its last four characters. Never the length, never the prefix.
    if (config.sensitiveFields.some(f => lowered.includes(f)) || /^\\d{4,}$/.test(text)) {
      return '***' + text.slice(-4);
    }
    return text.length > 40 ? text.slice(0, 40) + '…' : text;
  };

  const describe = (el) => {
    if (!el || !el.tagName) return { role: null, name: null };
    const control = el.closest('a,button,input,select,textarea,[role],label') || el;
    const label = control.closest('label');
    const name =
      control.getAttribute('aria-label') ||
      (label ? label.innerText.replace(control.innerText || '', '').trim() : '') ||
      (control.innerText || '').trim() ||
      control.getAttribute('name') ||
      null;
    const tag = control.tagName.toLowerCase();
    const role =
      control.getAttribute('role') ||
      ({ a: 'link', button: 'button', select: 'combobox', textarea: 'textbox' })[tag] ||
      (tag === 'input' ? 'textbox' : null);
    return { role, name: name ? name.slice(0, 80) : null };
  };

  // Hand each action straight to the host. An in-page buffer would be wiped by the very
  // navigations a human causes — clicking a submit button would erase the record of the click.
  const push = (type, el, value) => {
    const { role, name } = describe(el);
    try {
      window.__cuaRecordAction({
        type,
        role,
        name,
        value: value === undefined ? null : mask(el && el.getAttribute('name'), value),
        url: location.pathname,
        at: Math.round(performance.now()),
      });
    } catch (e) { /* the binding is gone; losing one action must not break the page */ }
  };

  document.addEventListener('click', e => push('click', e.target), true);
  document.addEventListener('change', e => push('change', e.target, e.target.value), true);
  // `input` fires per keystroke; record the field's final state on blur instead so the log reads
  // as "they filled this in", not a keylog.
  document.addEventListener('blur', e => {
    if (e.target && 'value' in e.target && e.target.value) push('input', e.target, e.target.value);
  }, true);
  // A navigation has no element. Passing document.body would describe it by the page's own text,
  // putting an arbitrary slab of the screen into the action log — and `mask` only covers `value`,
  // never `name`, so that text would arrive unmasked. The URL is the whole story here.
  push('navigation', null, undefined);
}
"""


@dataclass(frozen=True, slots=True)
class HumanAction:
    """One thing the human did, already masked."""

    type: str
    role: str | None
    name: str | None
    value: str | None
    url: str
    at_ms: int

    def describe(self) -> str:
        target = f"{self.role or '?'}: {self.name}" if self.name else (self.role or "page")
        return f"{self.type} {target}" + (f" = {self.value}" if self.value else "")


class HumanActionRecorder:
    """Installs the listener and drains what it captured."""

    def __init__(self, page: Any, sensitive_fields: frozenset[str] = frozenset()) -> None:
        self.page = page
        self.sensitive_fields = sensitive_fields
        self._installed = False
        self._actions: list[HumanAction] = []

    async def install(self) -> None:
        """Arm the listener for every document the human will visit, and the current one."""

        config = {"sensitiveFields": sorted(self.sensitive_fields)}
        if not self._installed:
            # The binding is the durable half: it is re-attached to every document on this page,
            # so actions survive the navigations a human causes.
            await self.page.expose_binding("__cuaRecordAction", self._receive)
            # `add_init_script` takes no argument parameter, so config is bound into an IIFE.
            await self.page.add_init_script(f"({_LISTENER_JS})({json.dumps(config)});")
            self._installed = True
        # `add_init_script` only affects documents loaded *after* it is added — the page the
        # human is already looking at needs the listener applied directly.
        await self.page.evaluate(_LISTENER_JS, config)

    def _receive(self, _source: Any, item: dict[str, Any]) -> None:
        self._actions.append(
            HumanAction(
                type=str(item.get("type", "unknown")),
                role=item.get("role"),
                name=item.get("name"),
                value=item.get("value") or None,
                url=str(item.get("url", "")),
                at_ms=int(item.get("at", 0)),
            )
        )

    async def drain(self) -> list[HumanAction]:
        """Take everything captured so far, in order."""

        captured, self._actions = self._actions, []
        return captured
