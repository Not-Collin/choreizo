"""Admin per-chore eligibility editor.

Lives in its own module to avoid bloating admin.py. Mounted under the
same /admin prefix.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.db import get_session
from app.eligibility import bulk_set_admin_eligibility, get_eligibility_map
from app.models import Chore, User

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

router = APIRouter(prefix="/admin/chores", tags=["admin"])


@router.get("/{chore_id}/eligibility", response_class=HTMLResponse)
async def chore_eligibility_form(
    chore_id: int,
    request: Request,
    saved: int = 0,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    chore = await session.get(Chore, chore_id)
    if chore is None:
        raise HTTPException(status_code=404)
    users = (
        await session.execute(select(User).order_by(User.active.desc(), User.name))
    ).scalars().all()
    modes = await get_eligibility_map(session, chore)
    return templates.TemplateResponse(
        request=request,
        name="admin/chore_eligibility.html",
        context={
            "current_user": admin,
            "chore": chore,
            "users": users,
            "modes": modes,
            "saved": bool(saved),
        },
    )


@router.post("/{chore_id}/eligibility")
async def chore_eligibility_save(
    chore_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    chore = await session.get(Chore, chore_id)
    if chore is None:
        raise HTTPException(status_code=404)
    form = await request.form()
    pairs: list[tuple[int, str]] = []
    for key, value in form.multi_items():
        if not key.startswith("mode_"):
            continue
        try:
            uid = int(key.removeprefix("mode_"))
        except ValueError:
            continue
        if value in {"allow", "deny", "default"}:
            pairs.append((uid, value))
    await bulk_set_admin_eligibility(session, chore=chore, modes=pairs)
    return RedirectResponse(
        f"/admin/chores/{chore_id}/eligibility?saved=1", status_code=303
    )
