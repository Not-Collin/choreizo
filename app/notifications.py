"""Pure DB logic for daily Telegram notifications.

Kept separate from app/tg/notify.py so the rules (who gets a send right
now? what does pressing a button do?) can be tested without touching
PTB or the network.

Vocabulary mapping between bot button payload and assignment status:

    button payload   stored status     completed_at?
    --------------   ---------------   --------------
    "done"           "completed"       set to now
    "skip"           "skipped"         not set
    "ignore"         "ignored"         not set

A "skipped" chore retries next cycle (per the locked-in PLAN decision);
an "ignored" chore is also treated as not-done but signals the assignee
explicitly bailed (Phase 8 escalation logic uses this).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Assignment, ReminderEvent, User

# A user's send_time is HH:MM. We send during the minute it matches and
# the next 4 minutes — the scheduler ticks every minute, but we want some
# tolerance so a brief outage doesn't skip the day.
SEND_WINDOW_MINUTES = 5

ACTION_TO_STATUS = {
    "done": "completed",
    "skip": "skipped",
    "ignore": "ignored",
}

ACTION_REPLY_TEMPLATE = {
    "done": "Marked as done.",
    "skip": "Skipped — it'll come around again next cycle.",
    "ignore": "Ignored.",
}


@dataclass
class SendBatch:
    user: User
    assignments: list[Assignment]


async def pending_assignments_for_user(
    session: AsyncSession,
    *,
    user_id: int,
    today_str: str,
) -> list[Assignment]:
    rows = (
        await session.execute(
            select(Assignment)
            .where(Assignment.user_id == user_id)
            .where(Assignment.assigned_date == today_str)
            .where(Assignment.status == "pending")
            .options(selectinload(Assignment.chore))
            .order_by(Assignment.id)
        )
    ).scalars().all()
    return list(rows)


async def _has_daily_send_today(
    session: AsyncSession,
    *,
    user_id: int,
    today_str: str,
) -> bool:
    """True if any assignment for this user today already has a daily_send."""
    row = (
        await session.execute(
            select(ReminderEvent.id)
            .join(Assignment, Assignment.id == ReminderEvent.assignment_id)
            .where(Assignment.user_id == user_id)
            .where(Assignment.assigned_date == today_str)
            .where(ReminderEvent.kind == "daily_send")
            .limit(1)
        )
    ).first()
    return row is not None


def _user_send_due(send_time: str, now_local: datetime) -> bool:
    """True iff `now_local`'s HH:MM is within SEND_WINDOW_MINUTES of send_time."""
    try:
        hh, mm = (int(x) for x in send_time.split(":"))
    except ValueError:
        return False
    target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    delta = (now_local - target).total_seconds()
    return 0 <= delta < SEND_WINDOW_MINUTES * 60


async def users_needing_send(
    session: AsyncSession,
    *,
    now_local: datetime,
) -> list[SendBatch]:
    """Return SendBatch per user whose daily send window opened just now."""
    today_str = now_local.date().isoformat()
    users = (
        await session.execute(
            select(User)
            .where(User.active.is_(True))
            .where(User.telegram_chat_id.is_not(None))
        )
    ).scalars().all()

    out: list[SendBatch] = []
    for user in users:
        if not _user_send_due(user.send_time, now_local):
            continue
        if await _has_daily_send_today(session, user_id=user.id, today_str=today_str):
            continue
        pending = await pending_assignments_for_user(
            session, user_id=user.id, today_str=today_str
        )
        if not pending:
            continue
        out.append(SendBatch(user=user, assignments=pending))
    return out


async def log_daily_send(
    session: AsyncSession,
    *,
    assignments: list[Assignment],
) -> None:
    for a in assignments:
        session.add(ReminderEvent(assignment_id=a.id, kind="daily_send"))
    await session.commit()


@dataclass
class ResponseResult:
    ok: bool
    message: str
    new_status: str | None


async def mark_chore_response(
    session: AsyncSession,
    *,
    assignment_id: int,
    action: str,
    by_chat_id: int,
) -> ResponseResult:
    """Apply a Done/Skip/Ignore press from the assigned user."""
    if action not in ACTION_TO_STATUS:
        return ResponseResult(False, f"Unknown action: {action}", None)

    a = (
        await session.execute(
            select(Assignment)
            .where(Assignment.id == assignment_id)
            .options(selectinload(Assignment.user), selectinload(Assignment.chore))
        )
    ).scalar_one_or_none()
    if a is None:
        return ResponseResult(False, "That chore is no longer in the system.", None)

    if a.user is None or a.user.telegram_chat_id != by_chat_id:
        return ResponseResult(False, "That chore was assigned to someone else.", None)

    if a.status != "pending":
        # Already responded; let the caller render the existing state.
        return ResponseResult(False, f"Already {a.status}.", a.status)

    new_status = ACTION_TO_STATUS[action]
    now = datetime.now(timezone.utc)
    a.status = new_status
    a.responded_at = now
    if action == "done":
        a.completed_at = now
    await session.commit()
    return ResponseResult(True, ACTION_REPLY_TEMPLATE[action], new_status)
