"""Admin: today's assignments + Run-now / Send-now / Force-send buttons."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.assignments import assign_for_date
from app.auth import require_admin
from app.config import get_settings
from app.db import AsyncSessionLocal, get_session
from app.models import Assignment, User

_VALID_STATUSES = {"pending", "completed", "skipped", "ignored", "overdue", "escalated"}
from app.tg.notify import chore_keyboard, chore_message_text

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

router = APIRouter(prefix="/admin", tags=["admin"])


def _today_in_house_tz() -> str:
    return datetime.now(ZoneInfo(get_settings().house_timezone)).date().isoformat()


@router.get("/assignments", response_class=HTMLResponse)
async def assignments_view(
    request: Request,
    saved: int = 0,
    sent: int = 0,
    reassigned: int = 0,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    today = _today_in_house_tz()
    today_rows = (
        await session.execute(
            select(Assignment)
            .where(Assignment.assigned_date == today)
            .options(selectinload(Assignment.chore), selectinload(Assignment.user))
            .order_by(Assignment.id)
        )
    ).scalars().all()
    recent_rows = (
        await session.execute(
            select(Assignment)
            .where(Assignment.assigned_date != today)
            .options(selectinload(Assignment.chore), selectinload(Assignment.user))
            .order_by(Assignment.assigned_date.desc(), Assignment.id.desc())
            .limit(30)
        )
    ).scalars().all()
    members = (
        await session.execute(
            select(User)
            .where(User.active.is_(True), User.is_admin.is_(False))
            .order_by(User.name)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="admin/assignments.html",
        context={
            "current_user": admin,
            "today": today,
            "today_rows": today_rows,
            "recent_rows": recent_rows,
            "members": members,
            "saved": bool(saved),
            "sent": int(sent),
            "reassigned": bool(reassigned),
        },
    )


# Test hooks — overridable in tests.
_session_factory = AsyncSessionLocal


async def _send_now_default() -> int:
    """Use the live scheduler tick (which respects send windows)."""
    from app.scheduler import tick_if_bot
    return await tick_if_bot()


_send_now = _send_now_default


@router.post("/assignments/{assignment_id}/reassign")
async def reassign_assignment(
    assignment_id: int,
    user_id: int = Form(...),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    a = await session.get(Assignment, assignment_id)
    if a is None:
        raise HTTPException(status_code=404)
    new_user = await session.get(User, user_id)
    if new_user is None or not new_user.active:
        raise HTTPException(status_code=400, detail="Invalid user.")
    a.user_id = user_id
    await session.commit()
    return RedirectResponse("/admin/assignments?reassigned=1", status_code=303)


@router.post("/assignments/{assignment_id}/status")
async def update_assignment_status(
    assignment_id: int,
    status: str = Form(...),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    if status not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status.")
    a = await session.get(Assignment, assignment_id)
    if a is None:
        raise HTTPException(status_code=404)
    a.status = status
    await session.commit()
    return RedirectResponse("/admin/assignments", status_code=303)


@router.post("/run-assignment")
async def run_assignment_now(admin: User = Depends(require_admin)):
    today = datetime.now(ZoneInfo(get_settings().house_timezone)).date()
    async with _session_factory() as session:
        await assign_for_date(session, today)
    return RedirectResponse("/admin/assignments?saved=1", status_code=303)


@router.post("/send-now")
async def send_now(admin: User = Depends(require_admin)):
    """Normal notify tick — only fires within each user's send window."""
    sent = await _send_now()
    return RedirectResponse(f"/admin/assignments?sent={sent}", status_code=303)


@router.post("/force-send")
async def force_send(admin: User = Depends(require_admin)):
    """Force-send today's pending assignments to ALL linked users right now.

    Bypasses the send-time window and the already-sent deduplication guard.
    Use this to test Telegram message formatting without waiting for the
    scheduled send window.
    """
    from app.scheduler import _active_bot

    bot = _active_bot
    if bot is None:
        return RedirectResponse("/admin/assignments?force_no_bot=1", status_code=303)

    settings = get_settings()
    today = datetime.now(ZoneInfo(settings.house_timezone)).date().isoformat()
    sent = 0

    async with AsyncSessionLocal() as session:
        users = (
            await session.execute(
                select(User)
                .where(User.active.is_(True))
                .where(User.telegram_chat_id.is_not(None))
            )
        ).scalars().all()

        for user in users:
            assignments = (
                await session.execute(
                    select(Assignment)
                    .where(Assignment.user_id == user.id)
                    .where(Assignment.assigned_date == today)
                    .where(Assignment.status == "pending")
                    .options(selectinload(Assignment.chore))
                    .order_by(Assignment.id)
                )
            ).scalars().all()

            if not assignments:
                continue

            try:
                await bot.send_message(
                    chat_id=user.telegram_chat_id,
                    text=f"📋 Test send — today's chores ({len(assignments)}):",
                )
                for a in assignments:
                    await bot.send_message(
                        chat_id=user.telegram_chat_id,
                        text=chore_message_text(a),
                        reply_markup=chore_keyboard(a.id),
                        parse_mode="Markdown",
                        disable_web_page_preview=True,
                    )
                sent += 1
            except Exception:
                pass  # best-effort; don't block the redirect

    return RedirectResponse(f"/admin/assignments?sent={sent}", status_code=303)
