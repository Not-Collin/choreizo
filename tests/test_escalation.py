"""Phase 8 tests: high-priority rollover, hourly reminders, escalation, admin notify."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.escalation import build_action_plan
from app.models import Assignment, Chore, ReminderEvent, User
from app.tg.escalation import apply_action_plan


def _utc(year=2026, month=4, day=24, hour=12, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


async def _seed_basic(
    s, *, hours_ago: int, priority: int = 1, with_admin: bool = True,
    assigned_date: str = "2026-04-24",
):
    """One assignee + chore + pending assignment, anchored to _utc()."""
    user = User(name="alice", active=True, telegram_chat_id=11)
    s.add(user)
    if with_admin:
        admin = User(name="boss", is_admin=True, active=True, telegram_chat_id=22)
        s.add(admin)
    chore = Chore(name="Trash", frequency_days=1, priority=priority, enabled=True)
    s.add(chore)
    await s.commit()
    await s.refresh(user); await s.refresh(chore)
    a = Assignment(
        chore_id=chore.id, user_id=user.id,
        assigned_date=assigned_date, status="pending",
        assigned_at=_utc() - timedelta(hours=hours_ago),
    )
    s.add(a); await s.commit(); await s.refresh(a)
    return user, chore, a


# -- Hourly reminders (high-priority only, same-day) --------------------------


@pytest.mark.asyncio
async def test_no_reminder_before_first_interval(db_factory) -> None:
    async with db_factory() as s:
        await _seed_basic(s, hours_ago=2)  # default interval is 3
        plan = await build_action_plan(s, now_utc=_utc(), today_str="2026-04-24")
        assert plan.hourly == []


@pytest.mark.asyncio
async def test_first_reminder_at_interval(db_factory) -> None:
    async with db_factory() as s:
        await _seed_basic(s, hours_ago=4)
        plan = await build_action_plan(s, now_utc=_utc(), today_str="2026-04-24")
        assert len(plan.hourly) == 1


@pytest.mark.asyncio
async def test_second_reminder_only_after_second_interval(db_factory) -> None:
    async with db_factory() as s:
        _, _, a = await _seed_basic(s, hours_ago=4)
        s.add(ReminderEvent(assignment_id=a.id, kind="hourly_reminder"))
        await s.commit()
        plan = await build_action_plan(s, now_utc=_utc(), today_str="2026-04-24")
        assert plan.hourly == []


@pytest.mark.asyncio
async def test_normal_priority_skips_hourly(db_factory) -> None:
    async with db_factory() as s:
        await _seed_basic(s, hours_ago=10, priority=0)
        plan = await build_action_plan(s, now_utc=_utc(), today_str="2026-04-24")
        assert plan.hourly == []


# -- Escalation (use priority=0 so rollover branch doesn't preempt) ----------


@pytest.mark.asyncio
async def test_escalation_after_threshold(db_factory) -> None:
    async with db_factory() as s:
        await _seed_basic(s, hours_ago=25, priority=0, assigned_date="2026-04-24")
        backup = User(name="backup", active=True, is_escalation=True, telegram_chat_id=33)
        s.add(backup); await s.commit()
        plan = await build_action_plan(s, now_utc=_utc(), today_str="2026-04-24")
        assert len(plan.escalations) == 1
        assert plan.escalations[0].new_user.name == "backup"


@pytest.mark.asyncio
async def test_escalation_only_once(db_factory) -> None:
    async with db_factory() as s:
        _, _, a = await _seed_basic(s, hours_ago=25, priority=0)
        backup = User(name="backup", active=True, is_escalation=True)
        s.add(backup); await s.commit()
        s.add(ReminderEvent(assignment_id=a.id, kind="escalation"))
        await s.commit()
        plan = await build_action_plan(s, now_utc=_utc(), today_str="2026-04-24")
        assert plan.escalations == []


@pytest.mark.asyncio
async def test_escalation_skipped_when_no_other_user(db_factory) -> None:
    async with db_factory() as s:
        await _seed_basic(s, hours_ago=25, priority=0, with_admin=False)
        plan = await build_action_plan(s, now_utc=_utc(), today_str="2026-04-24")
        assert plan.escalations == []


# -- Admin notify -------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_notify_after_threshold(db_factory) -> None:
    async with db_factory() as s:
        await _seed_basic(s, hours_ago=37, priority=0)
        plan = await build_action_plan(s, now_utc=_utc(), today_str="2026-04-24")
        assert len(plan.admin_notifies) == 1
        assert plan.admin_notifies[0].admin.name == "boss"


@pytest.mark.asyncio
async def test_admin_notify_only_once(db_factory) -> None:
    async with db_factory() as s:
        _, _, a = await _seed_basic(s, hours_ago=37, priority=0)
        s.add(ReminderEvent(assignment_id=a.id, kind="admin_notify"))
        await s.commit()
        plan = await build_action_plan(s, now_utc=_utc(), today_str="2026-04-24")
        assert plan.admin_notifies == []


# -- Rollover -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollover_for_high_priority_yesterday(db_factory) -> None:
    async with db_factory() as s:
        await _seed_basic(s, hours_ago=20, priority=1, assigned_date="2026-04-23")
        plan = await build_action_plan(s, now_utc=_utc(), today_str="2026-04-24")
        assert len(plan.rollovers) == 1


@pytest.mark.asyncio
async def test_no_rollover_for_normal_priority(db_factory) -> None:
    async with db_factory() as s:
        await _seed_basic(s, hours_ago=20, priority=0, assigned_date="2026-04-23")
        plan = await build_action_plan(s, now_utc=_utc(), today_str="2026-04-24")
        assert plan.rollovers == []


# -- apply_action_plan --------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_writes_reminder_events_and_messages(db_factory) -> None:
    async with db_factory() as s:
        await _seed_basic(s, hours_ago=4, priority=1)
        plan = await build_action_plan(s, now_utc=_utc(), today_str="2026-04-24")
        assert len(plan.hourly) == 1

    bot = MagicMock(); bot.send_message = AsyncMock()
    counts = await apply_action_plan(bot, plan, session_factory=db_factory)
    assert counts["hourly"] == 1
    assert bot.send_message.call_count == 1
    async with db_factory() as s:
        events = (
            await s.execute(select(ReminderEvent).where(ReminderEvent.kind == "hourly_reminder"))
        ).scalars().all()
        assert len(events) == 1


@pytest.mark.asyncio
async def test_apply_escalation_creates_new_assignment(db_factory) -> None:
    async with db_factory() as s:
        await _seed_basic(s, hours_ago=25, priority=0)
        backup = User(name="backup", active=True, is_escalation=True, telegram_chat_id=33)
        s.add(backup); await s.commit()
        plan = await build_action_plan(s, now_utc=_utc(), today_str="2026-04-24")

    bot = MagicMock(); bot.send_message = AsyncMock()
    counts = await apply_action_plan(bot, plan, session_factory=db_factory)
    assert counts["escalations"] == 1
    async with db_factory() as s:
        all_assignments = (await s.execute(select(Assignment))).scalars().all()
        statuses = {x.status for x in all_assignments}
        assert "escalated" in statuses
        new = next(x for x in all_assignments if x.status == "pending")
        assert new.escalated_from_user_id is not None


@pytest.mark.asyncio
async def test_apply_rollover_creates_today_row(db_factory) -> None:
    async with db_factory() as s:
        await _seed_basic(s, hours_ago=20, priority=1, assigned_date="2026-04-23")
        plan = await build_action_plan(s, now_utc=_utc(), today_str="2026-04-24")

    bot = MagicMock(); bot.send_message = AsyncMock()
    counts = await apply_action_plan(bot, plan, session_factory=db_factory)
    assert counts["rollovers"] == 1
    async with db_factory() as s:
        rows = (await s.execute(select(Assignment).order_by(Assignment.id))).scalars().all()
        assert rows[0].status == "overdue"
        assert rows[1].status == "pending"
        assert rows[1].assigned_date == "2026-04-24"
        assert rows[1].rolled_over_from_assignment_id == rows[0].id


@pytest.mark.asyncio
async def test_apply_is_idempotent_against_already_resolved(db_factory) -> None:
    async with db_factory() as s:
        _, _, a = await _seed_basic(s, hours_ago=25, priority=0)
        backup = User(name="backup", active=True, is_escalation=True)
        s.add(backup); await s.commit()
        plan = await build_action_plan(s, now_utc=_utc(), today_str="2026-04-24")
        a.status = "completed"
        await s.commit()

    bot = MagicMock(); bot.send_message = AsyncMock()
    counts = await apply_action_plan(bot, plan, session_factory=db_factory)
    assert counts["escalations"] == 0
    async with db_factory() as s:
        rows = (await s.execute(select(Assignment))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "completed"


# -- Snooze behaviour ---------------------------------------------------------


@pytest.mark.asyncio
async def test_snoozed_assignment_skips_hourly_reminder(db_factory) -> None:
    """An actively snoozed assignment should not appear in plan.hourly."""
    async with db_factory() as s:
        _, _, a = await _seed_basic(s, hours_ago=4)  # would normally get a reminder
        a.snoozed_until = _utc() + timedelta(hours=1)  # still snoozed
        await s.commit()
        plan = await build_action_plan(s, now_utc=_utc(), today_str="2026-04-24")
        assert plan.hourly == []


@pytest.mark.asyncio
async def test_expired_snooze_appears_in_snooze_resends(db_factory) -> None:
    """An assignment whose snooze has expired should appear in plan.snooze_resends."""
    async with db_factory() as s:
        _, _, a = await _seed_basic(s, hours_ago=2, priority=0)
        a.snoozed_until = _utc() - timedelta(minutes=30)  # expired 30 min ago
        await s.commit()
        plan = await build_action_plan(s, now_utc=_utc(), today_str="2026-04-24")
        assert len(plan.snooze_resends) == 1
        assert plan.snooze_resends[0].assignment.id == a.id


@pytest.mark.asyncio
async def test_apply_snooze_resend_sends_and_clears(db_factory) -> None:
    """apply_action_plan sends the chore message and clears snoozed_until."""
    async with db_factory() as s:
        _, _, a = await _seed_basic(s, hours_ago=2, priority=0)
        a.snoozed_until = _utc() - timedelta(minutes=30)
        await s.commit()
        plan = await build_action_plan(s, now_utc=_utc(), today_str="2026-04-24")

    bot = MagicMock(); bot.send_message = AsyncMock()
    counts = await apply_action_plan(bot, plan, session_factory=db_factory)
    assert counts["snooze_resends"] == 1
    bot.send_message.assert_called()

    # snoozed_until should be cleared so it doesn't fire again
    async with db_factory() as s:
        refreshed = (await s.execute(select(Assignment).where(Assignment.id == a.id))).scalar_one()
        assert refreshed.snoozed_until is None
