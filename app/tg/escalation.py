"""Apply an escalation/reminder ActionPlan: send DMs and write events.

Pure-DB writers + a thin Bot wrapper. Tests target the writers directly
with a mocked Bot so we don't need a live PTB Application.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.escalation import ActionPlan, build_action_plan
from app.models import Assignment, ReminderEvent
from app.tg.notify import chore_keyboard, chore_message_text

if TYPE_CHECKING:
    from telegram import Bot

log = logging.getLogger("choreizo.tg.escalation")


def _hourly_reminder_text(a: Assignment) -> str:
    return f"⏰ Reminder: this high-priority chore is still pending.\n\n{chore_message_text(a)}"


def _escalation_text(a: Assignment) -> str:
    return (
        "📨 You're being asked to cover this chore — the original "
        "assignee hasn't responded.\n\n" + chore_message_text(a)
    )


def _admin_notify_text(a: Assignment, assignee_name: str) -> str:
    return (
        f"🚨 Admin notice: {assignee_name} hasn't completed *{a.chore.name}* "
        f"after the configured SLA. You may want to nudge them."
    )


async def _send_safe(bot: "Bot", **kwargs) -> None:
    try:
        await bot.send_message(**kwargs)
    except Exception:  # pragma: no cover - PTB/network errors
        log.exception("Failed to send message to %s", kwargs.get("chat_id"))


async def apply_action_plan(
    bot: "Bot",
    plan: ActionPlan,
    *,
    session_factory=None,
) -> dict:
    """Run the plan in a single new session. Returns counts per kind."""
    factory = session_factory or AsyncSessionLocal
    counts = {"hourly": 0, "escalations": 0, "admin_notifies": 0, "rollovers": 0, "snooze_resends": 0}

    async with factory() as session:
        # 1. Hourly reminders.
        for hr in plan.hourly:
            chat_id = hr.assignment.user.telegram_chat_id if hr.assignment.user else None
            if chat_id is not None:
                await _send_safe(
                    bot,
                    chat_id=chat_id,
                    text=_hourly_reminder_text(hr.assignment),
                    reply_markup=chore_keyboard(hr.assignment.id),
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            session.add(ReminderEvent(assignment_id=hr.assignment.id, kind="hourly_reminder"))
            counts["hourly"] += 1

        # 2. Escalations.
        for esc in plan.escalations:
            old = await session.get(Assignment, esc.old_assignment.id)
            if old is None or old.status != "pending":
                continue
            old.status = "escalated"
            old.responded_at = datetime.now(timezone.utc)

            new = Assignment(
                chore_id=old.chore_id,
                user_id=esc.new_user.id,
                assigned_date=old.assigned_date,
                status="pending",
                escalated_from_user_id=old.user_id,
            )
            session.add(new)
            await session.flush()

            session.add(ReminderEvent(assignment_id=old.id, kind="escalation"))
            counts["escalations"] += 1

            # Try to load chore for the message.
            await session.refresh(new, attribute_names=["chore"])
            new_chat = esc.new_user.telegram_chat_id
            if new_chat is not None:
                await _send_safe(
                    bot,
                    chat_id=new_chat,
                    text=_escalation_text(new),
                    reply_markup=chore_keyboard(new.id),
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )

        # 3. Admin notifies.
        for an in plan.admin_notifies:
            chat_id = an.admin.telegram_chat_id
            if chat_id is not None:
                assignee_name = an.assignment.user.name if an.assignment.user else "the assignee"
                await _send_safe(
                    bot,
                    chat_id=chat_id,
                    text=_admin_notify_text(an.assignment, assignee_name),
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            session.add(ReminderEvent(assignment_id=an.assignment.id, kind="admin_notify"))
            counts["admin_notifies"] += 1

        # 4. Rollovers.
        for ro in plan.rollovers:
            old = await session.get(Assignment, ro.old_assignment.id)
            if old is None or old.status != "pending":
                continue
            old.status = "overdue"
            new = Assignment(
                chore_id=old.chore_id,
                user_id=old.user_id,  # same user — they still owe it
                assigned_date=ro.today_str,
                status="pending",
                rolled_over_from_assignment_id=old.id,
            )
            session.add(new)
            counts["rollovers"] += 1

        # 5. Snooze resends — timer expired, re-notify and clear the flag.
        for sr in plan.snooze_resends:
            a = await session.get(Assignment, sr.assignment.id)
            if a is None or a.status != "pending":
                continue
            chat_id = sr.assignment.user.telegram_chat_id if sr.assignment.user else None
            if chat_id is not None:
                await _send_safe(
                    bot,
                    chat_id=chat_id,
                    text=chore_message_text(sr.assignment),
                    reply_markup=chore_keyboard(sr.assignment.id),
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            # Clear the snooze so it doesn't fire again next tick.
            a.snoozed_until = None
            counts["snooze_resends"] += 1

        await session.commit()

    if any(counts.values()):
        log.info("Applied plan: %s", counts)
    return counts


async def hourly_tick(bot: "Bot", *, now_utc: datetime | None = None) -> dict:
    """One pass of build + apply. Used by the scheduler hourly job."""
    settings = get_settings()
    now = now_utc or datetime.now(timezone.utc)
    today_local = datetime.now(ZoneInfo(settings.house_timezone)).date().isoformat()
    async with AsyncSessionLocal() as session:
        plan = await build_action_plan(session, now_utc=now, today_str=today_local)
    if plan.is_empty:
        return {"hourly": 0, "escalations": 0, "admin_notifies": 0, "rollovers": 0, "snooze_resends": 0}
    return await apply_action_plan(bot, plan)
