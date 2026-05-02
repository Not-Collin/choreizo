"""Phase 6 tests: assignment engine + Run-now button."""
from __future__ import annotations

import random
from collections import Counter
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.assignments import JITTER_MIN_FREQ, assign_for_date, _compute_next_due
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
            # Reset state per trial: drop today's assignments and clear any
            # jitter-set next_due_date so the chore stays eligible.
            await s.execute(
                Assignment.__table__.delete().where(Assignment.assigned_date == "2026-04-24")
            )
            await s.execute(
                Chore.__table__.update()
                .where(Chore.name == "tg")
                .values(next_due_date=None)
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


# -- Jitter / spreading -------------------------------------------------------


def test_compute_next_due_short_cycle_exact() -> None:
    """Chores with freq < JITTER_MIN_FREQ always get exact scheduling."""
    chore = Chore(name="dishes", frequency_days=1, enabled=True)
    today = date(2026, 5, 1)
    rng = random.Random(0)
    result = _compute_next_due(chore, today, rng, set())
    assert result == today + timedelta(days=1)


def test_compute_next_due_long_cycle_within_jitter_window() -> None:
    """Long-cycle chore gets a date within ±10 % of frequency."""
    chore = Chore(name="filters", frequency_days=120, enabled=True)
    today = date(2026, 5, 1)
    rng = random.Random(0)
    occupied: set[date] = set()
    results = {_compute_next_due(chore, today, rng, occupied) for _ in range(50)}
    center = today + timedelta(days=120)
    jitter = round(120 * 0.10)  # 12
    for d in results:
        assert center - timedelta(days=jitter) <= d <= center + timedelta(days=jitter)


def test_compute_next_due_avoids_occupied_dates() -> None:
    """When center is occupied, result should differ."""
    chore = Chore(name="gutters", frequency_days=90, enabled=True)
    today = date(2026, 5, 1)
    center = today + timedelta(days=90)
    jitter = round(90 * 0.10)  # 9
    # Mark every day in the jitter range EXCEPT center+jitter as occupied.
    occupied = {center + timedelta(days=i) for i in range(-jitter, jitter)}
    rng = random.Random(0)
    result = _compute_next_due(chore, today, rng, occupied)
    assert result not in occupied


@pytest.mark.asyncio
async def test_assign_sets_next_due_date_with_jitter(db_factory) -> None:
    """assign_for_date should write a jitter-computed next_due_date on the chore."""
    async with db_factory() as s:
        u = User(name="a", active=True)
        chore = Chore(name="filters", frequency_days=30, enabled=True)
        s.add_all([u, chore])
        await s.commit()
        await s.refresh(chore)

        today = date(2026, 5, 1)
        await assign_for_date(s, today, rng=random.Random(7))
        await s.refresh(chore)

        assert chore.next_due_date is not None
        result = date.fromisoformat(chore.next_due_date)
        center = today + timedelta(days=30)
        jitter = round(30 * 0.10)  # 3
        assert center - timedelta(days=jitter) <= result <= center + timedelta(days=jitter)


@pytest.mark.asyncio
async def test_assign_spreads_high_freq_chores(db_factory) -> None:
    """Two high-frequency chores due on the same day should get different next_due dates."""
    async with db_factory() as s:
        u = User(name="a", active=True)
        c1 = Chore(name="filters",  frequency_days=90, enabled=True)
        c2 = Chore(name="gutters",  frequency_days=90, enabled=True)
        s.add_all([u, c1, c2])
        await s.commit()
        await s.refresh(c1); await s.refresh(c2)

        today = date(2026, 5, 1)
        await assign_for_date(s, today, rng=random.Random(0))
        await s.refresh(c1); await s.refresh(c2)

        assert c1.next_due_date is not None
        assert c2.next_due_date is not None
        assert c1.next_due_date != c2.next_due_date, (
            "High-frequency chores should be spread to different next-due dates"
        )


# -- HTTP: Admin reassign -----------------------------------------------------


def test_admin_reassign_assignment(client: TestClient, db_factory) -> None:
    import asyncio

    import app.web.routes.admin_assignments as mod
    mod._session_factory = db_factory

    async def _seed() -> tuple[int, int]:
        from app.auth import hash_password
        async with db_factory() as s:
            member_a = User(name="alice", active=True, password_hash=hash_password("pw"))
            member_b = User(name="bob",   active=True, password_hash=hash_password("pw"))
            chore    = Chore(name="Mop", frequency_days=7, enabled=True)
            s.add_all([member_a, member_b, chore])
            await s.commit()
            await s.refresh(member_a); await s.refresh(member_b); await s.refresh(chore)
            a = Assignment(
                chore_id=chore.id, user_id=member_a.id,
                assigned_date="2026-05-01", status="pending",
            )
            s.add(a); await s.commit(); await s.refresh(a)
            return a.id, member_b.id
    aid, bob_id = asyncio.get_event_loop().run_until_complete(_seed())

    client.post("/login", data={"name": "admin", "password": "hunter2"})
    r = client.post(
        f"/admin/assignments/{aid}/reassign",
        data={"user_id": str(bob_id)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "reassigned=1" in r.headers["location"]

    async def _check() -> str:
        async with db_factory() as s:
            a = (await s.execute(select(Assignment).where(Assignment.id == aid))).scalar_one()
            u = await s.get(User, a.user_id)
            return u.name
    assert asyncio.get_event_loop().run_until_complete(_check()) == "bob"
