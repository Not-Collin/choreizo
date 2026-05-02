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
        context={"current_user": admin, "chores": rows, "freq_display": _freq_display},
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


_UNIT_DAYS: dict[str, int] = {"day": 1, "week": 7, "month": 30, "year": 365}


def _freq_to_parts(days: int) -> tuple[int, str]:
    for unit in ("year", "month", "week", "day"):
        d = _UNIT_DAYS[unit]
        if days % d == 0:
            return days // d, unit
    return days, "day"


def _freq_display(days: int) -> str:
    n, unit = _freq_to_parts(days)
    return f"every {n} {unit}{'s' if n != 1 else ''}"


def _parse_weekdays(values: list[str]) -> str | None:
    days = sorted({int(d) for d in values if d.isdigit() and 0 <= int(d) <= 6})
    if len(days) == 0 or len(days) == 7:
        return None
    return ",".join(str(d) for d in days)


def _parse_months(values: list[str]) -> str | None:
    months = sorted({int(m) for m in values if m.isdigit() and 1 <= int(m) <= 12})
    if len(months) == 0 or len(months) == 12:
        return None
    return ",".join(str(m) for m in months)


def _parse_chore_form(
    name: str,
    description: str | None,
    frequency_amount: int,
    frequency_unit: str,
    priority: int,
    estimated_minutes: str | None,
    enabled: str | None,
    next_due_date: str | None = None,
    allowed_weekday_values: list[str] | None = None,
    allowed_month_values: list[str] | None = None,
) -> dict:
    unit_days = _UNIT_DAYS.get(frequency_unit, 1)
    frequency_days = max(1, frequency_amount * unit_days)
    if priority not in (0, 1):
        raise ValueError("Priority must be normal or high.")
    ndd = (next_due_date or "").strip() or None
    return {
        "name": name.strip(),
        "description": (description or "").strip() or None,
        "frequency_days": frequency_days,
        "priority": priority,
        "estimated_minutes": int(estimated_minutes) if estimated_minutes else None,
        "enabled": enabled is not None,
        "next_due_date": ndd,
        "allowed_weekdays": _parse_weekdays(allowed_weekday_values or []),
        "allowed_months": _parse_months(allowed_month_values or []),
    }


@router.post("/chores")
async def chore_create(
    request: Request,
    name: str = Form(...),
    description: str | None = Form(None),
    frequency_amount: int = Form(1),
    frequency_unit: str = Form("week"),
    priority: int = Form(0),
    estimated_minutes: str | None = Form(None),
    next_due_date: str | None = Form(None),
    enabled: str | None = Form(None),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    weekday_values = list(form.getlist("allowed_weekday"))
    month_values = list(form.getlist("allowed_month"))
    try:
        fields = _parse_chore_form(
            name, description, frequency_amount, frequency_unit,
            priority, estimated_minutes, enabled, next_due_date,
            weekday_values, month_values,
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
    month_set = (
        {int(m) for m in chore.allowed_months.split(",") if m.strip().isdigit()}
        if chore.allowed_months else None
    )
    freq_amount, freq_unit = _freq_to_parts(chore.frequency_days)
    return templates.TemplateResponse(
        request=request,
        name="admin/chore_form.html",
        context={
            "current_user": admin,
            "chore": chore,
            "form": {},
            "weekday_set": weekday_set,
            "month_set": month_set,
            "freq_amount": freq_amount,
            "freq_unit": freq_unit,
        },
    )


@router.post("/chores/{chore_id}")
async def chore_update(
    chore_id: int,
    request: Request,
    name: str = Form(...),
    description: str | None = Form(None),
    frequency_amount: int = Form(1),
    frequency_unit: str = Form("week"),
    priority: int = Form(0),
    estimated_minutes: str | None = Form(None),
    next_due_date: str | None = Form(None),
    enabled: str | None = Form(None),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    chore = await session.get(Chore, chore_id)
    if chore is None:
        raise HTTPException(status_code=404)
    form = await request.form()
    weekday_values = list(form.getlist("allowed_weekday"))
    month_values = list(form.getlist("allowed_month"))
    try:
        fields = _parse_chore_form(
            name, description, frequency_amount, frequency_unit,
            priority, estimated_minutes, enabled, next_due_date,
            weekday_values, month_values,
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
    if user.id == admin.id and user.active:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself.")
    user.active = not user.active
    await session.commit()
    return RedirectResponse("/admin/members", status_code=303)


@router.post("/members/{user_id}/toggle-admin")
async def member_toggle_admin(
    user_id: int,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404)
    if user.id == admin.id and user.is_admin:
        raise HTTPException(status_code=400, detail="Cannot remove your own admin role.")
    user.is_admin = not user.is_admin
    await session.commit()
    return RedirectResponse("/admin/members", status_code=303)
