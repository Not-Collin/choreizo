"""Admin Phase-9 reports: /admin/history + /admin/stats."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.config import get_settings
from app.db import get_session
from app.models import Chore, User
from app.stats import (
    completion_rate_per_chore,
    completion_rate_per_user,
    recent_assignments,
    total_assignment_count,
)

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

router = APIRouter(prefix="/admin", tags=["admin"])

PAGE_SIZE = 25


def _today_local() -> datetime:
    return datetime.now(ZoneInfo(get_settings().house_timezone))


@router.get("/history", response_class=HTMLResponse)
async def history_view(
    request: Request,
    user_id: int | None = None,
    chore_id: int | None = None,
    offset: int = 0,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    rows = await recent_assignments(
        session, limit=PAGE_SIZE, offset=offset,
        user_id=user_id or None, chore_id=chore_id or None,
    )
    total = await total_assignment_count(
        session, user_id=user_id or None, chore_id=chore_id or None,
    )
    users = (await session.execute(select(User).order_by(User.name))).scalars().all()
    chores = (await session.execute(select(Chore).order_by(Chore.name))).scalars().all()

    base_qs = {}
    if user_id:
        base_qs["user_id"] = user_id
    if chore_id:
        base_qs["chore_id"] = chore_id
    qs_prev = urlencode({**base_qs, "offset": max(0, offset - PAGE_SIZE)})
    qs_next = urlencode({**base_qs, "offset": offset + PAGE_SIZE})

    return templates.TemplateResponse(
        request=request,
        name="admin/history.html",
        context={
            "current_user": admin,
            "rows": rows,
            "users": users,
            "chores": chores,
            "user_id": user_id,
            "chore_id": chore_id,
            "offset": offset,
            "total": total,
            "page_size": PAGE_SIZE,
            "qs_prev": qs_prev,
            "qs_next": qs_next,
        },
    )


@router.get("/stats", response_class=HTMLResponse)
async def stats_view(
    request: Request,
    window_days: int = 30,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    window_days = max(1, min(365, window_days))
    since = (_today_local() - timedelta(days=window_days)).date().isoformat()
    user_rows = await completion_rate_per_user(session, since_date_str=since)
    chore_rows = await completion_rate_per_chore(session, since_date_str=since)
    return templates.TemplateResponse(
        request=request,
        name="admin/stats.html",
        context={
            "current_user": admin,
            "window_days": window_days,
            "user_rows": user_rows,
            "chore_rows": chore_rows,
        },
    )
