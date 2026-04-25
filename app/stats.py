"""Read-only aggregations for the admin reports pages.

All functions take a session and the lookback `since_date_str` (YYYY-MM-DD)
so callers can choose their own window without coupling to wall-clock
time.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Assignment, Chore, User

# Statuses that count toward "completed" in the rate.
DONE_STATUSES = {"completed"}


@dataclass
class CompletionRow:
    name: str
    total: int
    completed: int
    pending: int
    skipped: int
    ignored: int
    other: int

    @property
    def completion_rate(self) -> float:
        return (self.completed / self.total) if self.total else 0.0


async def _grouped_status_counts(
    session: AsyncSession,
    *,
    group_col,
    join_target,
    since_date_str: str,
) -> list[tuple[int, str, str, int]]:
    """Return [(group_id, group_name, status, count), ...] since the given date."""
    stmt = (
        select(group_col, join_target.name, Assignment.status, func.count())
        .join(join_target, group_col == join_target.id)
        .where(Assignment.assigned_date >= since_date_str)
        .group_by(group_col, join_target.name, Assignment.status)
    )
    return list((await session.execute(stmt)).all())


def _rollup(rows: list[tuple[int, str, str, int]]) -> list[CompletionRow]:
    by_group: dict[int, CompletionRow] = {}
    for group_id, group_name, status, n in rows:
        row = by_group.get(group_id)
        if row is None:
            row = CompletionRow(name=group_name, total=0, completed=0, pending=0,
                                skipped=0, ignored=0, other=0)
            by_group[group_id] = row
        row.total += n
        if status == "completed":
            row.completed += n
        elif status == "pending":
            row.pending += n
        elif status == "skipped":
            row.skipped += n
        elif status == "ignored":
            row.ignored += n
        else:
            row.other += n
    return sorted(by_group.values(), key=lambda r: -r.completion_rate)


async def completion_rate_per_user(
    session: AsyncSession, *, since_date_str: str
) -> list[CompletionRow]:
    raw = await _grouped_status_counts(
        session,
        group_col=Assignment.user_id,
        join_target=User,
        since_date_str=since_date_str,
    )
    return _rollup(raw)


async def completion_rate_per_chore(
    session: AsyncSession, *, since_date_str: str
) -> list[CompletionRow]:
    raw = await _grouped_status_counts(
        session,
        group_col=Assignment.chore_id,
        join_target=Chore,
        since_date_str=since_date_str,
    )
    return _rollup(raw)


async def recent_assignments(
    session: AsyncSession,
    *,
    limit: int,
    offset: int = 0,
    user_id: int | None = None,
    chore_id: int | None = None,
) -> list[Assignment]:
    stmt = (
        select(Assignment)
        .options(selectinload(Assignment.chore), selectinload(Assignment.user))
        .order_by(Assignment.assigned_date.desc(), Assignment.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if user_id is not None:
        stmt = stmt.where(Assignment.user_id == user_id)
    if chore_id is not None:
        stmt = stmt.where(Assignment.chore_id == chore_id)
    return list((await session.execute(stmt)).scalars().all())


async def total_assignment_count(
    session: AsyncSession,
    *,
    user_id: int | None = None,
    chore_id: int | None = None,
) -> int:
    stmt = select(func.count(Assignment.id))
    if user_id is not None:
        stmt = stmt.where(Assignment.user_id == user_id)
    if chore_id is not None:
        stmt = stmt.where(Assignment.chore_id == chore_id)
    return int((await session.execute(stmt)).scalar_one())
