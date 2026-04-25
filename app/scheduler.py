"""APScheduler wiring — runs on the FastAPI/PTB event loop.

Schedules:
  daily_assignment   — cron at settings.daily_assignment_hour
  notify_tick        — every minute; daily DM send when a user's send_time
                       window opens (no-op when no bot)
  hourly_escalation  — every hour; runs the high-priority/escalation
                       behaviours (no-op when no bot)
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.assignments import assign_for_date
from app.config import get_settings
from app.db import AsyncSessionLocal

log = logging.getLogger("choreizo.sched")

_active_bot = None


def set_active_bot(bot) -> None:
    global _active_bot
    _active_bot = bot


def clear_active_bot() -> None:
    global _active_bot
    _active_bot = None


async def run_daily_assignment() -> int:
    settings = get_settings()
    today = datetime.now(ZoneInfo(settings.house_timezone)).date()
    async with AsyncSessionLocal() as session:
        created = await assign_for_date(session, today)
    log.info("Daily assignment for %s created %d new rows", today, len(created))
    return len(created)


async def tick_if_bot() -> int:
    if _active_bot is None:
        return 0
    from app.tg.notify import tick

    return await tick(_active_bot)


async def hourly_tick_if_bot() -> dict:
    if _active_bot is None:
        return {}
    from app.tg.escalation import hourly_tick

    return await hourly_tick(_active_bot)


def build_scheduler() -> AsyncIOScheduler:
    settings = get_settings()
    tz = ZoneInfo(settings.house_timezone)
    sched = AsyncIOScheduler(timezone=tz)
    sched.add_job(
        run_daily_assignment,
        CronTrigger(hour=settings.daily_assignment_hour, minute=0, timezone=tz),
        id="daily_assignment",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        tick_if_bot,
        IntervalTrigger(minutes=1, timezone=tz),
        id="notify_tick",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        hourly_tick_if_bot,
        CronTrigger(minute=5, timezone=tz),  # 5 minutes past every hour
        id="hourly_escalation",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return sched
