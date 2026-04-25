"""Admin panel routes — Chores + Members CRUD.

Every route depends on `require_admin`, which 401s when unauthenticated
and 403s when an authenticated non-admin tries to enter. A small wrapper
below converts those 401s into a 303 redirect to /login so browser-driven
admins land on the sign-in page rather than seeing a JSON error.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.db import get_session
from app.models import Chore, User

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(_admin: User = Depends(require_admin)):
    # Phase 3 keeps the dashboard as a thin redirect to chores; later
    # phases swap this for an at-a-glance overview.
    return RedirectResponse("/admin/chores", status_code=303)


# ---------------------------------------------------------------------------
# Chores
# ---------------------------------------------------------------------------


@router.get("/chores", response_class=HTMLResponse)
async def chores_list(
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    rows = (
        await session.execute(select(Chore).order_by(Chore.enabled.desc(), Chore.name))
    ).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="admin/chores_list.html",
        context={"current_user": admin, "chores": rows},
    )


@router.get("/chores/new", response_class=HTMLResponse)
async def chore_new_form(
    request: Request,
    admin: User = Depends(require_admin),
):
    return templates.TemplateResponse(
        request=request,
        name="admin/chore_form.html",
        context={"current_user": admin, "chore": None, "form": {}},
    )


def _parse_weekdays(values: list[str]) -> str | None:
    days = sorted({int(d) for d in values if d.isdigit() and 0 <= int(d) <= 6})
    if len(days) == 0 or len(days) == 7:
        return None
    return ",".join(str(d) for d in days)


def _parse_chore_form(
    name: str,
    description: str | None,
    frequency_days: int,
    priority: int,
    estimated_minutes: str | None,
    enabled: str | None,
    allowed_weekday_values: list[str] | None = None,
) -> dict:
    if frequency_days < 1:
        raise ValueError("Frequency must be at least 1 day.")
    if priority not in (0, 1):
        raise ValueError("Priority must be normal or high.")
    return {
        "name": name.strip(),
        "description": (description or "").strip() or None,
        "frequency_days": frequency_days,
        "priority": priority,
        "estimated_minutes": int(estimated_minutes) if estimated_minutes else None,
        "enabled": enabled is not None,
        "allowed_weekdays": _parse_weekdays(allowed_weekday_values or []),
    }


@router.post("/chores")
async def chore_create(
    request: Request,
    name: str = Form(...),
    description: str | None = Form(None),
    frequency_days: int = Form(...),
    priority: int = Form(0),
    estimated_minutes: str | None = Form(None),
    enabled: str | None = Form(None),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    weekday_values = list(form.getlist("allowed_weekday"))
    try:
        fields = _parse_chore_form(
            name, description, frequency_days, priority, estimated_minutes, enabled,
            weekday_values,
        )
    except ValueError as e:
        return templates.TemplateResponse(
            request=request,
            name="admin/chore_form.html",
            context={
                "current_user": admin,
                "chore": None,
                "form": form,
                "error": str(e),
            },
            status_code=400,
        )
    chore = Chore(created_by_user_id=admin.id, **fields)
    session.add(chore)
    await session.commit()
    return RedirectResponse("/admin/chores", status_code=303)


@router.get("/chores/{chore_id}/edit", response_class=HTMLResponse)
async def chore_edit_form(
    chore_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    chore = await session.get(Chore, chore_id)
    if chore is None:
        raise HTTPException(status_code=404)
    weekday_set = (
        {int(d) for d in chore.allowed_weekdays.split(",") if d.strip().isdigit()}
        if chore.allowed_weekdays else None
    )
    return templates.TemplateResponse(
        request=request,
        name="admin/chore_form.html",
        context={"current_user": admin, "chore": chore, "form": {}, "weekday_set": weekday_set},
    )


@router.post("/chores/{chore_id}")
async def chore_update(
    chore_id: int,
    request: Request,
    name: str = Form(...),
    description: str | None = Form(None),
    frequency_days: int = Form(...),
    priority: int = Form(0),
    estimated_minutes: str | None = Form(None),
    enabled: str | None = Form(None),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    chore = await session.get(Chore, chore_id)
    if chore is None:
        raise HTTPException(status_code=404)
    form = await request.form()
    weekday_values = list(form.getlist("allowed_weekday"))
    try:
        fields = _parse_chore_form(
            name, description, frequency_days, priority, estimated_minutes, enabled,
            weekday_values,
        )
    except ValueError as e:
        return templates.TemplateResponse(
            request=request,
            name="admin/chore_form.html",
            context={
                "current_user": admin,
                "chore": chore,
                "form": form,
                "error": str(e),
            },
            status_code=400,
        )
    for k, v in fields.items():
        setattr(chore, k, v)
    await session.commit()
    return RedirectResponse("/admin/chores", status_code=303)


@router.post("/chores/{chore_id}/delete")
async def chore_delete(
    chore_id: int,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    chore = await session.get(Chore, chore_id)
    if chore is None:
        raise HTTPException(status_code=404)
    await session.delete(chore)
    await session.commit()
    return RedirectResponse("/admin/chores", status_code=303)


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


@router.get("/members", response_class=HTMLResponse)
async def members_list(
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    rows = (
        await session.execute(
            select(User).order_by(User.is_admin.desc(), User.active.desc(), User.name)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="admin/members_list.html",
        context={"current_user": admin, "users": rows},
    )


@router.post("/members/{user_id}/toggle")
async def member_toggle(
    user_id: int,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404)
    # Don't let an admin lock themselves out.
    if user.id == admin.id and user.active:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself.")
    user.active = not user.active
    await session.commit()
    return RedirectResponse("/admin/members", status_code=303)
