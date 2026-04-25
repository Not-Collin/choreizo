"""Plan the four high-priority behaviours (pure DB read).

Each behaviour fires off the *original* assignment timestamps + the
reminder_events log so the planner is idempotent: re-running the same
hour produces the same plan only if no events were written between
runs, and once an event is logged the matching action drops off.

The four kinds and their thresholds (all configurable):

  hourly_reminder  — every high_priority_reminder_interval_hours after
                     assigned_at, while still pending, ping the assignee
                     again. Logs `hourly_reminder`.
  escalation       — once the chore is older than escalation_after_hours
                     and still pending, hand the chore off to the
                     designated escalation user. Logs `escalation` once
                     and never re-fires.
  admin_notify     — once older than admin_notify_after_hours and still
                     pending (post-escalation counts), DM the first
                     active admin. Logs `admin_notify` once.
  rollover         — at day-change, if a high-priority chore is still
                     pending from yesterday, mark it 'overdue' and emit
                     a fresh assignment for `today` (no reminder_event
                     needed — the original row's status flip is the
                     idempotency token).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.models import Assignment, ReminderEvent, User


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class HourlyReminder:
    assignment: Assignment


@dataclass
class Escalation:
    old_assignment: Assignment
    new_user: User


@dataclass
class AdminNotify:
    assignment: Assignment
    admin: User


@dataclass
class Rollover:
    """The original is marked overdue and a fresh row is created for today."""
    old_assignment: Assignment
    today_str: str


@dataclass
class ActionPlan:
    hourly: list[HourlyReminder] = field(default_factory=list)
    escalations: list[Escalation] = field(default_factory=list)
    admin_notifies: list[AdminNotify] = field(default_factory=list)
    rollovers: list[Rollover] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.hourly or self.escalations or self.admin_notifies or self.rollovers)


async def _reminder_kinds_for(
    session: AsyncSession, assignment_ids: list[int]
) -> dict[int, set[str]]:
    if not assignment_ids:
        return {}
    rows = (
        await session.execute(
            select(ReminderEvent.assignment_id, ReminderEvent.kind).where(
                ReminderEvent.assignment_id.in_(assignment_ids)
            )
        )
    ).all()
    out: dict[int, set[str]] = {aid: set() for aid in assignment_ids}
    for aid, kind in rows:
        out.setdefault(aid, set()).add(kind)
    return out


async def _hourly_reminder_count(
    session: AsyncSession, assignment_id: int
) -> int:
    rows = (
        await session.execute(
            select(ReminderEvent.id).where(
                ReminderEvent.assignment_id == assignment_id,
                ReminderEvent.kind == "hourly_reminder",
            )
        )
    ).all()
    return len(rows)


async def _pick_escalation_user(session: AsyncSession, exclude_user_id: int) -> User | None:
    """Find an active is_escalation user (preferred) or any other admin."""
    user = (
        await session.execute(
            select(User)
            .where(User.is_escalation.is_(True), User.active.is_(True), User.id != exclude_user_id)
            .order_by(User.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if user is not None:
        return user
    return (
        await session.execute(
            select(User)
            .where(User.is_admin.is_(True), User.active.is_(True), User.id != exclude_user_id)
            .order_by(User.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _pick_admin(session: AsyncSession) -> User | None:
    return (
        await session.execute(
            select(User)
            .where(User.is_admin.is_(True), User.active.is_(True))
            .order_by(User.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def build_action_plan(
    session: AsyncSession,
    *,
    now_utc: datetime | None = None,
    today_str: str,
) -> ActionPlan:
    settings = get_settings()
    now = now_utc or _now_utc()
    plan = ActionPlan()

    pending = (
        await session.execute(
            select(Assignment)
            .where(Assignment.status == "pending")
            .options(selectinload(Assignment.chore), selectinload(Assignment.user))
            .order_by(Assignment.id)
        )
    ).scalars().all()
    if not pending:
        return plan

    kinds_per_assignment = await _reminder_kinds_for(session, [a.id for a in pending])
    admin = await _pick_admin(session)

    for a in pending:
        chore = a.chore
        assigned_at = _aware(a.assigned_at)
        age_hours = (now - assigned_at).total_seconds() / 3600.0
        kinds = kinds_per_assignment.get(a.id, set())

        # 1. ROLLOVER: still pending from a prior day and high-priority.
        if (
            settings.rollover_high_priority
            and chore is not None
            and chore.priority == 1
            and a.assigned_date < today_str
        ):
            plan.rollovers.append(Rollover(old_assignment=a, today_str=today_str))
            continue  # don't also nag the original; the rolled-over copy nags

        # 2. HOURLY REMINDER (high-priority only).
        if chore is not None and chore.priority == 1:
            interval = max(1, settings.high_priority_reminder_interval_hours)
            sent_count = await _hourly_reminder_count(session, a.id)
            # Each reminder represents `interval` hours of waiting; we owe
            # a fresh ping when (sent_count + 1) * interval <= age_hours.
            if (sent_count + 1) * interval <= age_hours:
                plan.hourly.append(HourlyReminder(assignment=a))

        # 3. ESCALATION: only once.
        if (
            "escalation" not in kinds
            and age_hours >= settings.escalation_after_hours
        ):
            new_user = await _pick_escalation_user(session, exclude_user_id=a.user_id)
            if new_user is not None and new_user.id != a.user_id:
                plan.escalations.append(Escalation(old_assignment=a, new_user=new_user))

        # 4. ADMIN NOTIFY: only once, and only to an active admin.
        if (
            "admin_notify" not in kinds
            and age_hours >= settings.admin_notify_after_hours
            and admin is not None
        ):
            plan.admin_notifies.append(AdminNotify(assignment=a, admin=admin))

    return plan
