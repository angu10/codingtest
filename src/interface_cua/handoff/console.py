"""Operator console — the smallest thing that satisfies "a human takes over the same session".

There is no co-browsing here and no remote desktop. The human already has the browser: replay runs
it headed, and the console's job is only to show *why* it stopped and to take a decision. That
keeps the escalation path honest — the operator drives the actual Chromium window, with the real
cookies and the real session, and the lease guarantees automation is not also acting.

The production form (containerised Chromium behind noVNC, queued requests, per-tenant RBAC) is
described in REPORT §5 as a designed seam and deliberately not built.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from interface_cua.handoff.intervention import HandoffCoordinator

CONSOLE_PORT = 8765

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Operator console</title>
<meta http-equiv="refresh" content="3">
<style>
 body{{font:15px/1.5 -apple-system,system-ui,sans-serif;margin:0;background:#f5f6f7;color:#18202a}}
 .wrap{{max-width:760px;margin:40px auto;background:#fff;border:1px solid #d3d7db;padding:28px}}
 h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#66707a;margin:0 0 24px;font-size:13px}}
 dl{{display:grid;grid-template-columns:150px 1fr;gap:8px 16px;margin:0 0 24px}}
 dt{{color:#66707a}} dd{{margin:0;font-family:ui-monospace,monospace;font-size:13px}}
 .reason{{background:#fff4d6;border-left:4px solid #a66b00;padding:12px 14px;margin:0 0 24px}}
 form{{display:inline}} button{{font:inherit;padding:9px 18px;border:1px solid #143a5b;cursor:pointer}}
 .go{{background:#245b88;color:#fff}} .stop{{background:#fff;color:#821b1b;border-color:#821b1b}}
 .idle{{color:#66707a}} ul{{padding-left:20px}} li{{font-family:ui-monospace,monospace;font-size:13px}}
</style></head><body><div class="wrap">{body}</div></body></html>"""

_OPEN = """
<h1>Automation is paused</h1>
<p class="sub">You have control of the browser window. Finish what is needed, then choose below.</p>
<div class="reason"><strong>{reason}</strong></div>
<dl>
  <dt>capability</dt><dd>{capability_id}</dd>
  <dt>stopped at step</dt><dd>{step_id}</dd>
  <dt>current URL</dt><dd>{url}</dd>
  <dt>run</dt><dd>{run_id}</dd>
  <dt>controller</dt><dd>{controller}</dd>
</dl>
<form method="post" action="/resume"><button class="go" type="submit">Resume automation</button></form>
&nbsp;
<form method="post" action="/abort"><button class="stop" type="submit">Abort run</button></form>
<p class="sub" style="margin-top:24px">Resuming re-checks the step's precondition before
continuing. If you left the session somewhere the capability cannot continue from, it stops
again rather than trusting the step number.</p>
"""

_RESOLVED = """
<h1>Decision recorded: {resolution}</h1>
<p class="sub">Actions captured while you held control (values masked at the point of capture):</p>
<ul>{actions}</ul>
"""

_IDLE = '<h1>No intervention pending</h1><p class="sub idle">Waiting for a run to escalate.</p>'


def build_console(coordinator: HandoffCoordinator) -> FastAPI:
    app = FastAPI(title="Operator console")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        request = coordinator.request
        if request is None:
            return HTMLResponse(_PAGE.format(body=_IDLE))
        if request.resolution is not None:
            actions = "".join(f"<li>{a.describe()}</li>" for a in request.human_actions) or (
                "<li class='idle'>none recorded</li>"
            )
            return HTMLResponse(
                _PAGE.format(body=_RESOLVED.format(resolution=request.resolution, actions=actions))
            )
        return HTMLResponse(
            _PAGE.format(
                body=_OPEN.format(
                    reason=request.reason,
                    capability_id=request.capability_id,
                    step_id=request.step_id,
                    url=request.url,
                    run_id=request.run_id,
                    controller=coordinator.controller.value,
                )
            )
        )

    @app.get("/intervention")
    async def intervention() -> JSONResponse:
        request = coordinator.request
        return JSONResponse(request.as_dict() if request else {"status": "idle"})

    @app.post("/resume", response_class=HTMLResponse)
    async def resume() -> Any:
        await coordinator.resolve("resume")
        return await index()

    @app.post("/abort", response_class=HTMLResponse)
    async def abort() -> Any:
        await coordinator.resolve("abort")
        return await index()

    return app
