"""Meridian CU Servicing Console: a stable but intentionally hostile target UI."""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from demo_app.data import MEMBERS

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")

app = FastAPI(title="Meridian CU Servicing Console")
app.state.detail_delay_seconds = 6.0


@app.get("/", response_class=HTMLResponse)
async def member_search(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "search.html", {})


@app.post("/lookup/r7q9x", response_class=HTMLResponse)
async def lookup_member(request: Request, member_id: str = Form(...)) -> HTMLResponse:
    member_id = member_id.strip()
    if member_id == "99999" or member_id not in MEMBERS:
        return TEMPLATES.TemplateResponse(
            request,
            "outcome.html",
            {
                "heading": "No member found",
                "detail": "No member matches the reference you entered.",
            },
            status_code=404,
        )
    return RedirectResponse(f"/member/{member_id}", status_code=303)


@app.get("/member/{member_id}", response_class=HTMLResponse)
async def member_detail(request: Request, member_id: str) -> HTMLResponse:
    member = _member_or_404(member_id)
    if member.get("delay"):
        await asyncio.sleep(app.state.detail_delay_seconds)
    return TEMPLATES.TemplateResponse(
        request,
        "member.html",
        {"member": member, "member_id": member_id},
    )


@app.get("/account-pane/{member_id}", response_class=HTMLResponse)
async def account_pane(
    request: Request, member_id: str, maintenance_dismissed: bool = False
) -> HTMLResponse:
    member = _member_or_404(member_id)
    # These two denials are deliberately near-identical: same heading, same styling, same status,
    # one sentence apart. One is a legitimate answer about the member; the other is a defect in our
    # own entitlements. Nothing in the rendered page says which. Only the capability contract does.
    if member.get("restricted"):
        return TEMPLATES.TemplateResponse(
            request,
            "pane_error.html",
            {
                "heading": "Permission denied",
                "detail": "This member record is restricted and cannot be serviced.",
            },
            status_code=403,
        )
    if member.get("authorization_denied"):
        return TEMPLATES.TemplateResponse(
            request,
            "pane_error.html",
            {
                "heading": "Permission denied",
                "detail": "Your operator role lacks the servicing entitlement.",
            },
            status_code=403,
        )
    if member.get("session_expired"):
        return TEMPLATES.TemplateResponse(
            request,
            "pane_error.html",
            {
                "heading": "Session expired",
                "detail": "Sign in again to restore the authenticated session.",
            },
            status_code=401,
        )
    show_modal = bool(member.get("maintenance_modal") and not maintenance_dismissed)
    return TEMPLATES.TemplateResponse(
        request,
        "account_pane.html",
        {"member": member, "member_id": member_id, "show_modal": show_modal},
    )


@app.get("/service/x4m9p/{member_id}/{account_id}", response_class=HTMLResponse)
async def sub_account_form(request: Request, member_id: str, account_id: str) -> HTMLResponse:
    member = _member_or_404(member_id)
    account = _account_or_404(member, account_id)
    return TEMPLATES.TemplateResponse(
        request,
        "sub_account_form.html",
        {"member": member, "member_id": member_id, "account": account},
    )


@app.post("/service/x4m9p/{member_id}/{account_id}/review", response_class=HTMLResponse)
async def review_sub_account(
    request: Request,
    member_id: str,
    account_id: str,
    account_type: str = Form(...),
    nickname: str = Form(""),
    opening_deposit: str = Form("0.00"),
) -> HTMLResponse:
    member = _member_or_404(member_id)
    account = _account_or_404(member, account_id)
    if account_type not in {"savings", "money_market"}:
        raise HTTPException(status_code=422, detail="Unsupported account type")
    try:
        deposit = Decimal(opening_deposit).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise HTTPException(status_code=422, detail="Invalid opening deposit") from exc
    if deposit < 0:
        raise HTTPException(status_code=422, detail="Opening deposit cannot be negative")
    review = {
        "account_type": account_type,
        "nickname": nickname[:40],
        "opening_deposit": str(deposit),
    }
    return TEMPLATES.TemplateResponse(
        request,
        "review.html",
        {"member": member, "member_id": member_id, "account": account, "review": review},
    )


@app.post("/service/q2c8v/create", response_class=HTMLResponse)
async def create_sub_account(
    request: Request,
    member_id: str = Form(...),
    account_id: str = Form(...),
    account_type: str = Form(...),
    nickname: str = Form(""),
    opening_deposit: str = Form("0.00"),
) -> HTMLResponse:
    member = _member_or_404(member_id)
    _account_or_404(member, account_id)
    created = {
        "account_type": account_type,
        "nickname": nickname[:40],
        "opening_deposit": opening_deposit,
    }
    if created not in member["sub_accounts"]:
        member["sub_accounts"].append(created)

    # The write has landed before this response fails. Replay must probe, never click again.
    if member.get("ambiguous_create"):
        return TEMPLATES.TemplateResponse(
            request,
            "create_ambiguous.html",
            {},
            status_code=500,
        )
    return TEMPLATES.TemplateResponse(request, "created.html", {"created": created})


@app.get("/api/members/{member_id}/sub-accounts", response_class=JSONResponse)
async def list_sub_accounts(member_id: str) -> dict[str, object]:
    """Independent read-only probe used to reconcile an ambiguous create."""

    member = _member_or_404(member_id)
    return {"member_id": member_id, "sub_accounts": member["sub_accounts"]}


def _member_or_404(member_id: str) -> dict[str, object]:
    member = MEMBERS.get(member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    return member


def _account_or_404(member: dict[str, object], account_id: str) -> dict[str, str]:
    for account in member["accounts"]:  # type: ignore[union-attr]
        if account["id"] == account_id:
            return account
    raise HTTPException(status_code=404, detail="Account not found")

