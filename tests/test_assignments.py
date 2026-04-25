"""Phase 6 tests: assignment engine + Run-now button."""
from __future__ import annotations

import random
from collections import Counter
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.assignments import assign_for_date
from app.models import Assignment, Chore, ChoreEligibility, User


# -- Pure engine --------------------------------------------------------------


@pytest.mark.asyncio
async def test_assigns_one_per_due_chore(db_factory) -> None:
    async with db_factory() as s:
        s.add_all([
            User(name="a", active=True),
            User(name="b", active=True),
            Chore(name="Vacuum", frequency_days=7, enabled=True),
            Chore(name="Mop",    frequency_days=14, enabled=True),
            Chore(name="Off",    frequency_days=1,  enabled=False),
        ])
        await s.commit()

        created = await assign_for_date(s, date(2026, 4, 24), rng=random.Random(0))
        assert len(created) == 2  # disabled chore is skipped
        assert {c.chore_name for c in created} == {"Vacuum", "Mop"}


@pytest.mark.asyncio
async def test_idempotent_same_day(db_factory) -> None:
    async with db_factory() as s:
        s.add_all([
            User(name="a", active=True),
            Chore(name="Vacuum", frequency_days=7, enabled=True),
        ])
        await s.commit()
        rng = random.Random(42)

        first = await assign_for_date(s, date(2026, 4, 24), rng=rng)
        second = await assign_for_date(s, date(2026, 4, 24), rng=rng)
        assert len(first) == 1
        assert second == []
        rows = (await s.execute(select(Assignment))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_skips_chores_within_frequency_window(db_factory) -> None:
    async with db_factory() as s:
        u = User(name="a", active=True)
        chore = Chore(name="Weekly", frequency_days=7, enabled=True)
        s.add_all([u, chore])
        await s.commit()
        await s.refresh(u); await s.refresh(chore)
        s.add(Assignment(
            chore_id=chore.id, user_id=u.id,
            assigned_date="2026-04-20", status="completed",
        ))
        await s.commit()

        # 4 days later -> still inside the 7-day window.
        out = await assign_for_date(s, date(2026, 4, 24), rng=random.Random(1))
        assert out == []

        # 7 days later -> due again.
        out = await assign_for_date(s, date(2026, 4, 27), rng=random.Random(1))
        assert len(out) == 1


@pytest.mark.asyncio
async def test_respects_eligibility(db_factory) -> None:
    async with db_factory() as s:
        a = User(name="a", active=True)
        b = User(name="b", active=True)
        chore = Chore(name="Trash", frequency_days=1, enabled=True)
        s.add_all([a, b, chore])
        await s.commit()
        await s.refresh(a); await s.refresh(b); await s.refresh(chore)
        # b is denied; only a is eligible.
        s.add(ChoreEligibility(chore_id=chore.id, user_id=b.id, mode="deny"))
        await s.commit()

        out = await assign_for_date(s, date(2026, 4, 24), rng=random.Random(0))
        assert len(out) == 1
        assert out[0].user_name == "a"


@pytest.mark.asyncio
async def test_no_eligible_users_means_no_assignment(db_factory) -> None:
    async with db_factory() as s:
        a = User(name="a", active=False)  # inactive
        chore = Chore(name="Trash", frequency_days=1, enabled=True)
        s.add_all([a, chore])
        await s.commit()
        out = await assign_for_date(s, date(2026, 4, 24), rng=random.Random(0))
        assert out == []


@pytest.mark.asyncio
async def test_fairness_skews_to_lower_load(db_factory) -> None:
    """Over many trials the lighter-loaded user should win the majority."""
    async with db_factory() as s:
        light = User(name="light", active=True)
        heavy = User(name="heavy", active=True)
        s.add_all([light, heavy])
        await s.commit()
        await s.refresh(light); await s.refresh(heavy)
        # Pre-load `heavy` with 5 recent assignments via a dummy chore.
        dummy = Chore(name="dummy", frequency_days=1, enabled=False)
        s.add(dummy); await s.commit(); await s.refresh(dummy)
        for i in range(5):
            s.add(Assignment(
                chore_id=dummy.id, user_id=heavy.id,
                assigned_date=f"2026-04-1{i}",
                status="completed",
            ))
        await s.commit()

    # Many independent runs to estimate the win rate.
    wins = Counter()
    for seed in range(200):
        async with db_factory() as s:
            # Reset state per trial: drop today's assignments + chore.
            await s.execute(
                Assignment.__table__.delete().where(Assignment.assigned_date == "2026-04-24")
            )
            chore_id = (await s.execute(select(Chore.id).where(Chore.name == "tg"))).scalar_one_or_none()
            if chore_id is None:
                c = Chore(name="tg", frequency_days=1, enabled=True)
                s.add(c); await s.commit(); await s.refresh(c)
                chore_id = c.id
            await s.commit()
            out = await assign_for_date(s, date(2026, 4, 24), rng=random.Random(seed))
            if out:
                wins[out[0].user_name] += 1
    assert wins["light"] > wins["heavy"], wins
    assert wins["light"] > 1.5 * wins["heavy"], wins


# -- HTTP: Run-now button -----------------------------------------------------


def test_run_assignment_now_creates_rows(client: TestClient, db_factory) -> None:
    """Force the /admin/run-assignment route to use the test DB factory."""
    import asyncio
    import app.web.routes.admin_assignments as mod

    mod._session_factory = db_factory  # monkeypatch hook

    async def _seed() -> None:
        async with db_factory() as s:
            s.add_all([
                User(name="a", active=True),
                Chore(name="Vacuum", frequency_days=7, enabled=True),
            ])
            await s.commit()
    asyncio.get_event_loop().run_until_complete(_seed())

    r = client.post("/login", data={"name": "admin", "password": "hunter2"})
    assert r.status_code in (200, 303)

    r = client.post("/admin/run-assignment", follow_redirects=False)
    assert r.status_code == 303

    async def _count() -> int:
        async with db_factory() as s:
            return len((await s.execute(select(Assignment))).scalars().all())
    assert asyncio.get_event_loop().run_until_complete(_count()) >= 1

    page = client.get("/admin/assignments").text
    assert "Vacuum" in page
