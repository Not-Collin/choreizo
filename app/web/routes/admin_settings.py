"""Admin: settings — editable timing/URL values stored in app_settings table."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.config import (
    get_effective_bool,
    get_effective_int,
    get_effective_str,
    get_settings,
    set_runtime_override,
)
from app.db import get_session
from app.models import AppSetting, User

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

router = APIRouter(prefix="/admin", tags=["admin"])

EDITABLE_KEYS = {
    "base_url",
    "house_timezone",
    "daily_assignment_hour",
    "high_priority_reminder_interval_hours",
    "escalation_after_hours",
    "admin_notify_after_hours",
    "rollover_high_priority",
}


async def load_db_overrides(session: AsyncSession) -> None:
    """Read app_settings rows for editable keys and populate runtime overrides."""
    rows = (
        await session.execute(
            select(AppSetting).where(AppSetting.key.in_(EDITABLE_KEYS))
        )
    ).scalars().all()
    for row in rows:
        set_runtime_override(row.key, row.value)


def _effective(s) -> dict:
    return {
        "base_url": get_effective_str("base_url", s.base_url),
        "house_timezone": get_effective_str("house_timezone", s.house_timezone),
        "daily_assignment_hour": get_effective_int("daily_assignment_hour", s.daily_assignment_hour),
        "high_priority_reminder_interval_hours": get_effective_int(
            "high_priority_reminder_interval_hours", s.high_priority_reminder_interval_hours
        ),
        "escalation_after_hours": get_effective_int("escalation_after_hours", s.escalation_after_hours),
        "admin_notify_after_hours": get_effective_int("admin_notify_after_hours", s.admin_notify_after_hours),
        "rollover_high_priority": get_effective_bool("rollover_high_priority", s.rollover_high_priority),
    }


@router.get("/settings", response_class=HTMLResponse)
async def settings_view(
    request: Request,
    saved: int = 0,
    admin: User = Depends(require_admin),
):
    s = get_settings()
    return templates.TemplateResponse(
        request=request,
        name="admin/settings.html",
        context={
            "current_user": admin,
            "s": s,
            "eff": _effective(s),
            "bot_configured": bool(s.telegram_bot_token),
            "saved": bool(saved),
        },
    )


@router.post("/settings")
async def settings_save(
    request: Request,
    base_url: str = Form(...),
    house_timezone: str = Form(...),
    daily_assignment_hour: int = Form(...),
    high_priority_reminder_interval_hours: int = Form(...),
    escalation_after_hours: int = Form(...),
    admin_notify_after_hours: int = Form(...),
    rollover_high_priority: str | None = Form(None),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    updates = {
        "base_url": base_url.strip(),
        "house_timezone": house_timezone.strip(),
        "daily_assignment_hour": str(max(0, min(23, daily_assignment_hour))),
        "high_priority_reminder_interval_hours": str(max(1, high_priority_reminder_interval_hours)),
        "escalation_after_hours": str(max(1, escalation_after_hours)),
        "admin_notify_after_hours": str(max(1, admin_notify_after_hours)),
        "rollover_high_priority": "true" if rollover_high_priority else "false",
    }
    for key, value in updates.items():
        existing = await session.get(AppSetting, key)
        if existing is not None:
            existing.value = value
        else:
            session.add(AppSetting(key=key, value=value))
        set_runtime_override(key, value)
    await session.commit()
    return RedirectResponse("/admin/settings?saved=1", status_code=303)
