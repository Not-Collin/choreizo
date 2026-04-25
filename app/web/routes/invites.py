"""Admin invite-code routes (mounted under /admin/invites).

Kept separate from admin.py to avoid that module ballooning. The list
template needs the used_by_user relationship eager-loaded — async
sessions can't lazy-load.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import require_admin
from app.db import get_session
from app.invites import mint_invite
from app.models import InviteCode, User

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

router = APIRouter(prefix="/admin/invites", tags=["admin"])


@router.get("", response_class=HTMLResponse)
async def invites_list(
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    just_minted: str | None = None,
):
    rows = (
        await session.execute(
            select(InviteCode)
            .options(selectinload(InviteCode.used_by_user))
            .order_by(InviteCode.created_at.desc())
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="admin/invites_list.html",
        context={
            "current_user": admin,
            "codes": rows,
            "just_minted": just_minted,
        },
    )


@router.post("")
async def invites_create(
    intended_name: str | None = Form(None),
    is_admin: str | None = Form(None),
    ttl_hours: int = Form(72),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    code = await mint_invite(
        session,
        intended_name=intended_name,
        is_admin=is_admin is not None,
        ttl_hours=max(1, min(720, ttl_hours)),
    )
    return RedirectResponse(
        f"/admin/invites?just_minted={code.code}", status_code=303
    )


@router.post("/{code}/revoke")
async def invites_revoke(
    code: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(InviteCode, code)
    if row is None:
        raise HTTPException(status_code=404)
    if row.used_at is not None:
        raise HTTPException(status_code=400, detail="Already used; cannot revoke.")
    await session.delete(row)
    await session.commit()
    return RedirectResponse("/admin/invites", status_code=303)
