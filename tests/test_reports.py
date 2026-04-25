"""Phase 9 tests: stats helpers + history/stats HTTP."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models import Assignment, Chore, User
from app.stats import (
    completion_rate_per_chore,
    completion_rate_per_user,
    recent_assignments,
    total_assignment_count,
)


@pytest.mark.asyncio
async def test_completion_rate_per_user(db_factory) -> None:
    async with db_factory() as s:
        u1 = User(name="alice", active=True)
        u2 = User(name="bob",   active=True)
        c  = Chore(name="X", frequency_days=1)
        s.add_all([u1, u2, c]); await s.commit()
        await s.refresh(u1); await s.refresh(u2); await s.refresh(c)
        s.add_all([
            Assignment(chore_id=c.id, user_id=u1.id, assigned_date="2026-04-24", status="completed"),
            Assignment(chore_id=c.id, user_id=u1.id, assigned_date="2026-04-23", status="completed"),
            Assignment(chore_id=c.id, user_id=u1.id, assigned_date="2026-04-22", status="skipped"),
            Assignment(chore_id=c.id, user_id=u2.id, assigned_date="2026-04-24", status="ignored"),
        ])
        await s.commit()

        rows = await completion_rate_per_user(s, since_date_str="2026-04-01")
        by = {r.name: r for r in rows}
        assert by["alice"].total == 3
        assert by["alice"].completed == 2
        assert pytest.approx(by["alice"].completion_rate, abs=0.01) == 2/3
        assert by["bob"].completed == 0


@pytest.mark.asyncio
async def test_completion_rate_per_chore_filters_window(db_factory) -> None:
    async with db_factory() as s:
        u = User(name="a", active=True)
        c1 = Chore(name="In", frequency_days=1)
        c2 = Chore(name="Out", frequency_days=1)
        s.add_all([u, c1, c2]); await s.commit()
        await s.refresh(u); await s.refresh(c1); await s.refresh(c2)
        s.add_all([
            Assignment(chore_id=c1.id, user_id=u.id, assigned_date="2026-04-24", status="completed"),
            # Older than the window — should be excluded.
            Assignment(chore_id=c2.id, user_id=u.id, assigned_date="2026-04-01", status="completed"),
        ])
        await s.commit()

        rows = await completion_rate_per_chore(s, since_date_str="2026-04-20")
        names = {r.name for r in rows}
        assert names == {"In"}


@pytest.mark.asyncio
async def test_recent_assignments_paginates(db_factory) -> None:
    async with db_factory() as s:
        u = User(name="a", active=True)
        c = Chore(name="X", frequency_days=1)
        s.add_all([u, c]); await s.commit()
        await s.refresh(u); await s.refresh(c)
        for i in range(50):
            s.add(Assignment(chore_id=c.id, user_id=u.id,
                             assigned_date=f"2026-03-{(i % 28) + 1:02d}",
                             status="completed"))
        await s.commit()

        page1 = await recent_assignments(s, limit=10, offset=0)
        page2 = await recent_assignments(s, limit=10, offset=10)
        assert len(page1) == 10 == len(page2)
        # No overlap between pages.
        ids1 = {a.id for a in page1}
        ids2 = {a.id for a in page2}
        assert not (ids1 & ids2)
        assert await total_assignment_count(s) == 50


@pytest.mark.asyncio
async def test_recent_assignments_filters_user_and_chore(db_factory) -> None:
    async with db_factory() as s:
        u1 = User(name="a", active=True); u2 = User(name="b", active=True)
        c1 = Chore(name="X", frequency_days=1); c2 = Chore(name="Y", frequency_days=1)
        s.add_all([u1, u2, c1, c2]); await s.commit()
        await s.refresh(u1); await s.refresh(u2); await s.refresh(c1); await s.refresh(c2)
        s.add_all([
            Assignment(chore_id=c1.id, user_id=u1.id, assigned_date="2026-04-24", status="completed"),
            Assignment(chore_id=c2.id, user_id=u2.id, assigned_date="2026-04-24", status="completed"),
            Assignment(chore_id=c1.id, user_id=u2.id, assigned_date="2026-04-24", status="completed"),
        ])
        await s.commit()

        rows = await recent_assignments(s, limit=10, user_id=u1.id)
        assert len(rows) == 1
        assert rows[0].chore_id == c1.id

        rows = await recent_assignments(s, limit=10, chore_id=c1.id)
        assert len(rows) == 2


# -- HTTP ---------------------------------------------------------------------


def _login_admin(client: TestClient) -> None:
    r = client.post("/login", data={"name": "admin", "password": "hunter2"})
    assert r.status_code in (200, 303)


def test_history_page_renders(client: TestClient, db_factory) -> None:
    _login_admin(client)
    import asyncio

    async def _seed() -> None:
        async with db_factory() as s:
            u = User(name="member", active=True)
            c = Chore(name="Vacuum", frequency_days=7)
            s.add_all([u, c]); await s.commit()
            await s.refresh(u); await s.refresh(c)
            s.add(Assignment(chore_id=c.id, user_id=u.id,
                             assigned_date="2026-04-23", status="completed"))
            await s.commit()
    asyncio.get_event_loop().run_until_complete(_seed())

    r = client.get("/admin/history")
    assert r.status_code == 200
    assert "Vacuum" in r.text
    assert "member" in r.text


def test_stats_page_renders(client: TestClient, db_factory) -> None:
    _login_admin(client)
    import asyncio

    async def _seed() -> None:
        async with db_factory() as s:
            u = User(name="alice", active=True)
            c = Chore(name="Chop", frequency_days=1)
            s.add_all([u, c]); await s.commit()
            await s.refresh(u); await s.refresh(c)
            for st in ("completed", "completed", "skipped"):
                s.add(Assignment(chore_id=c.id, user_id=u.id,
                                 assigned_date="2026-04-24", status=st))
            await s.commit()
    asyncio.get_event_loop().run_until_complete(_seed())

    r = client.get("/admin/stats?window_days=30")
    assert r.status_code == 200
    assert "alice" in r.text
    assert "67%" in r.text  # 2/3 completion
