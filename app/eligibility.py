"""Per-chore eligibility resolution + writers.

Resolution rule (from PLAN.md §3 / models.ChoreEligibility docstring):

  * If a chore has at least one row with mode='allow', it operates as an
    allow-list: only those users are eligible (and they must be active).
  * Otherwise all active users are eligible EXCEPT users with mode='deny'.

Writers expose two narrowly-scoped helpers:

  * set_member_optin(chore, user, opted_in)
        Member-facing: a member toggling their checkbox writes a 'deny'
        row when opted_in=False and removes any 'deny' row when
        opted_in=True. It never writes 'allow' rows — those are admin-set.

  * set_admin_eligibility(chore, user, mode)
        Admin-facing: mode is 'allow' | 'deny' | 'default'.
        'default' deletes any existing row.
"""
from __future__ import annotations

from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Chore, ChoreEligibility, User


async def resolve_eligible_users(
    session: AsyncSession,
    chore: Chore,
) -> list[User]:
    """Return the active users currently eligible for `chore`."""
    rules = (
        await session.execute(
            select(ChoreEligibility)
            .where(ChoreEligibility.chore_id == chore.id)
            .options(selectinload(ChoreEligibility.user))
        )
    ).scalars().all()

    allow_users = [r.user for r in rules if r.mode == "allow"]
    deny_user_ids = {r.user_id for r in rules if r.mode == "deny"}

    if allow_users:
        return [u for u in allow_users if u.active]

    all_users = (
        await session.execute(select(User).where(User.active.is_(True)))
    ).scalars().all()
    return [u for u in all_users if u.id not in deny_user_ids]


async def get_eligibility_map(
    session: AsyncSession,
    chore: Chore,
) -> dict[int, str]:
    """user_id -> 'allow' | 'deny' for any explicit rules on this chore."""
    rules = (
        await session.execute(
            select(ChoreEligibility).where(ChoreEligibility.chore_id == chore.id)
        )
    ).scalars().all()
    return {r.user_id: r.mode for r in rules}


async def set_member_optin(
    session: AsyncSession,
    *,
    chore: Chore,
    user: User,
    opted_in: bool,
) -> None:
    """Member-facing: toggle a 'deny' row only.

    A member's "I'm willing to do this" checkbox doesn't override an
    admin's explicit allow-list — if the chore is in allow-list mode and
    the member isn't on the list, the member can't add themselves here.
    Phase 5 keeps that simple: members only manage their own deny rows.
    """
    existing = await session.get(ChoreEligibility, (chore.id, user.id))
    if opted_in:
        # Remove a deny row if it exists; ignore allow rows (admin owns those).
        if existing is not None and existing.mode == "deny":
            await session.delete(existing)
    else:
        if existing is None:
            session.add(ChoreEligibility(chore_id=chore.id, user_id=user.id, mode="deny"))
        elif existing.mode != "deny":
            existing.mode = "deny"
    await session.commit()


async def set_admin_eligibility(
    session: AsyncSession,
    *,
    chore: Chore,
    user: User,
    mode: str,
) -> None:
    """Admin-facing: set to 'allow' / 'deny' / 'default' (no row)."""
    if mode not in {"allow", "deny", "default"}:
        raise ValueError(f"Unknown eligibility mode: {mode!r}")
    existing = await session.get(ChoreEligibility, (chore.id, user.id))
    if mode == "default":
        if existing is not None:
            await session.delete(existing)
    elif existing is None:
        session.add(ChoreEligibility(chore_id=chore.id, user_id=user.id, mode=mode))
    else:
        existing.mode = mode
    await session.commit()


async def bulk_set_admin_eligibility(
    session: AsyncSession,
    *,
    chore: Chore,
    modes: Iterable[tuple[int, str]],
) -> None:
    """Apply many admin overrides in one transaction.

    `modes` is an iterable of (user_id, 'allow'|'deny'|'default') pairs.
    Used by the admin eligibility-editor form so the whole matrix saves
    atomically.
    """
    existing = {
        (e.chore_id, e.user_id): e
        for e in (
            await session.execute(
                select(ChoreEligibility).where(ChoreEligibility.chore_id == chore.id)
            )
        ).scalars()
    }
    for user_id, mode in modes:
        if mode not in {"allow", "deny", "default"}:
            continue
        row = existing.get((chore.id, user_id))
        if mode == "default":
            if row is not None:
                await session.delete(row)
        elif row is None:
            session.add(ChoreEligibility(chore_id=chore.id, user_id=user_id, mode=mode))
        else:
            row.mode = mode
    await session.commit()
