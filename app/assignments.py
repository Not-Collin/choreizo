"""Daily chore assignment engine.

Picks one assignee per due chore for a given date.

Due rule:
  A chore is due on `today` if either it has no prior assignment, or the
  most recent assigned_date is `frequency_days` or more days ago. The
  status of that prior assignment doesn't matter — completed, skipped,
  ignored all roll forward to the next cycle the same way. (Note: Phase
  8 will layer high-priority *intra-cycle* reminders on top, but a chore
  re-enters this scheduler exactly once per cycle either way.)

Fairness:
  Per-chore, weight every eligible user by `1 / (1 + recent_load)` where
  `recent_load` is the number of assignments that user already has on or
  after `today - LOAD_WINDOW_DAYS`. Lighter loads get heavier weights,
  so newcomers and recently-quiet members get picked first while still
  leaving room for randomness.

Idempotency:
  If an assignment already exists for `(chore, today)` we never create a
  second one — this lets the scheduler and the admin "Run now" button
  fire as many times as they like in a single day.
"""
from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.eligibility import resolve_eligible_users
from app.models import Assignment, Chore, User

LOAD_WINDOW_DAYS = 30


def _date_str(d: date) -> str:
    return d.isoformat()


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


@dataclass
class AssignmentSummary:
    chore_id: int
    chore_name: str
    user_id: int
    user_name: str
    assignment_id: int


async def _existing_today(
    session: AsyncSession, today_str: str
) -> set[int]:
    """Return the set of chore_ids that already have an assignment on `today`."""
    rows = (
        await session.execute(
            select(Assignment.chore_id).where(Assignment.assigned_date == today_str)
        )
    ).all()
    return {r[0] for r in rows}


async def _last_assignment_date(
    session: AsyncSession,
) -> dict[int, date]:
    """chore_id -> max(assigned_date) across the table."""
    rows = (
        await session.execute(
            select(Assignment.chore_id, Assignment.assigned_date)
        )
    ).all()
    out: dict[int, date] = {}
    for chore_id, ds in rows:
        d = _parse_date(ds)
        if chore_id not in out or out[chore_id] < d:
            out[chore_id] = d
    return out


async def _recent_load_per_user(
    session: AsyncSession, since: date
) -> dict[int, int]:
    """user_id -> assignment count since `since` (inclusive)."""
    since_str = _date_str(since)
    rows = (
        await session.execute(
            select(Assignment.user_id).where(Assignment.assigned_date >= since_str)
        )
    ).all()
    return Counter(r[0] for r in rows)


def _weighted_choice(
    candidates: list[User],
    loads: dict[int, int],
    rng: random.Random,
) -> User:
    weights = [1.0 / (1.0 + loads.get(u.id, 0)) for u in candidates]
    return rng.choices(candidates, weights=weights, k=1)[0]


async def assign_for_date(
    session: AsyncSession,
    today: date,
    *,
    rng: random.Random | None = None,
) -> list[AssignmentSummary]:
    """Create one Assignment per due chore. Idempotent for the same date.

    Returns the list of newly-created assignments (empty when nothing
    was due, or when re-run on a day that's already assigned out).
    """
    rng = rng or random.Random()
    today_str = _date_str(today)

    chores = (
        await session.execute(
            select(Chore).where(Chore.enabled.is_(True)).order_by(Chore.id)
        )
    ).scalars().all()

    already = await _existing_today(session, today_str)
    last_dates = await _last_assignment_date(session)
    loads = await _recent_load_per_user(session, today - timedelta(days=LOAD_WINDOW_DAYS))

    created: list[AssignmentSummary] = []

    for chore in chores:
        if chore.id in already:
            continue
        last = last_dates.get(chore.id)
        if last is not None and (today - last).days < chore.frequency_days:
            continue
        if chore.allowed_weekdays is not None:
            allowed = {int(d) for d in chore.allowed_weekdays.split(",") if d.strip().isdigit()}
            if today.weekday() not in allowed:
                continue

        eligible = await resolve_eligible_users(session, chore)
        if not eligible:
            continue

        winner = _weighted_choice(eligible, loads, rng)
        a = Assignment(
            chore_id=chore.id,
            user_id=winner.id,
            assigned_date=today_str,
            status="pending",
        )
        session.add(a)
        await session.flush()
        # Bump in-memory load so a chain of due chores in one run doesn't
        # all dog-pile the same low-load user.
        loads[winner.id] = loads.get(winner.id, 0) + 1
        created.append(AssignmentSummary(
            chore_id=chore.id, chore_name=chore.name,
            user_id=winner.id, user_name=winner.name,
            assignment_id=a.id,
        ))

    if created:
        await session.commit()
    return created
