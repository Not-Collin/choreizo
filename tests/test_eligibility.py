"""Phase 5 tests: eligibility resolver, /me/chores, admin matrix."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.eligibility import (
    bulk_set_admin_eligibility,
    get_eligibility_map,
    resolve_eligible_users,
    set_admin_eligibility,
    set_member_optin,
)
from app.models import Chore, ChoreEligibility, User


# -- Resolver -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_includes_all_active_users(db_factory) -> None:
    async with db_factory() as s:
        u1 = User(name="a", active=True)
        u2 = User(name="b", active=True)
        u3 = User(name="c", active=False)  # inactive
        chore = Chore(name="Vacuum", frequency_days=7)
        s.add_all([u1, u2, u3, chore])
        await s.commit()
        eligible = await resolve_eligible_users(s, chore)
        names = sorted(u.name for u in eligible)
        assert names == ["a", "b"]


@pytest.mark.asyncio
async def test_deny_excludes_user(db_factory) -> None:
    async with db_factory() as s:
        u1 = User(name="a", active=True)
        u2 = User(name="b", active=True)
        chore = Chore(name="X", frequency_days=1)
        s.add_all([u1, u2, chore])
        await s.commit()
        await s.refresh(u1)
        await s.refresh(chore)
        s.add(ChoreEligibility(chore_id=chore.id, user_id=u1.id, mode="deny"))
        await s.commit()
        eligible = await resolve_eligible_users(s, chore)
        assert [u.name for u in eligible] == ["b"]


@pytest.mark.asyncio
async def test_allow_list_mode_only_includes_allowed(db_factory) -> None:
    async with db_factory() as s:
        u1 = User(name="a", active=True)
        u2 = User(name="b", active=True)
        u3 = User(name="c", active=True)
        chore = Chore(name="X", frequency_days=1)
        s.add_all([u1, u2, u3, chore])
        await s.commit()
        # Only u1 explicitly allowed -> u2 and u3 are excluded.
        s.add(ChoreEligibility(chore_id=chore.id, user_id=u1.id, mode="allow"))
        await s.commit()
        eligible = await resolve_eligible_users(s, chore)
        assert [u.name for u in eligible] == ["a"]


@pytest.mark.asyncio
async def test_allow_list_skips_inactive(db_factory) -> None:
    async with db_factory() as s:
        u1 = User(name="a", active=False)
        chore = Chore(name="X", frequency_days=1)
        s.add_all([u1, chore])
        await s.commit()
        s.add(ChoreEligibility(chore_id=chore.id, user_id=u1.id, mode="allow"))
        await s.commit()
        eligible = await resolve_eligible_users(s, chore)
        assert eligible == []


# -- Member writers -----------------------------------------------------------


@pytest.mark.asyncio
async def test_set_member_optin_writes_and_clears_deny(db_factory) -> None:
    async with db_factory() as s:
        user = User(name="u", active=True)
        chore = Chore(name="c", frequency_days=1)
        s.add_all([user, chore])
        await s.commit()

        await set_member_optin(s, chore=chore, user=user, opted_in=False)
        modes = await get_eligibility_map(s, chore)
        assert modes == {user.id: "deny"}

        await set_member_optin(s, chore=chore, user=user, opted_in=True)
        modes = await get_eligibility_map(s, chore)
        assert modes == {}


@pytest.mark.asyncio
async def test_member_cannot_clobber_admin_allow(db_factory) -> None:
    async with db_factory() as s:
        user = User(name="u", active=True)
        chore = Chore(name="c", frequency_days=1)
        s.add_all([user, chore])
        await s.commit()
        s.add(ChoreEligibility(chore_id=chore.id, user_id=user.id, mode="allow"))
        await s.commit()

        # Member opt_in=True with an existing 'allow' row is a no-op.
        await set_member_optin(s, chore=chore, user=user, opted_in=True)
        modes = await get_eligibility_map(s, chore)
        assert modes == {user.id: "allow"}


# -- Admin writers ------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_bulk_set_round_trip(db_factory) -> None:
    async with db_factory() as s:
        u1 = User(name="a", active=True)
        u2 = User(name="b", active=True)
        u3 = User(name="c", active=True)
        chore = Chore(name="c", frequency_days=1)
        s.add_all([u1, u2, u3, chore])
        await s.commit()

        await bulk_set_admin_eligibility(
            s,
            chore=chore,
            modes=[(u1.id, "allow"), (u2.id, "deny"), (u3.id, "default")],
        )
        modes = await get_eligibility_map(s, chore)
        assert modes == {u1.id: "allow", u2.id: "deny"}

        # Flip back to default removes rows.
        await bulk_set_admin_eligibility(
            s,
            chore=chore,
            modes=[(u1.id, "default"), (u2.id, "default")],
        )
        assert await get_eligibility_map(s, chore) == {}


@pytest.mark.asyncio
async def test_admin_set_eligibility_rejects_bad_mode(db_factory) -> None:
    async with db_factory() as s:
        u1 = User(name="a")
        chore = Chore(name="c", frequency_days=1)
        s.add_all([u1, chore])
        await s.commit()
        with pytest.raises(ValueError):
            await set_admin_eligibility(s, chore=chore, user=u1, mode="bogus")


# -- HTTP: /me/chores ---------------------------------------------------------


def _make_member(db_factory, name="member") -> int:
    """Sync helper: create a member, return id."""
    import asyncio
    from app.auth import hash_password

    async def _go() -> int:
        async with db_factory() as s:
            u = User(name=name, active=True, is_admin=False, password_hash=hash_password("pw"))
            s.add(u)
            await s.commit()
            await s.refresh(u)
            return u.id

    return asyncio.get_event_loop().run_until_complete(_go())


def _login_member(client: TestClient, name: str = "member") -> None:
    r = client.post("/login", data={"name": name, "password": "pw"})
    assert r.status_code in (200, 303)


def test_me_chores_lists_enabled_chores(client: TestClient, db_factory) -> None:
    _make_member(db_factory)
    _login_member(client)

    # Seed two chores via the admin UI is overkill — use direct SQL.
    import asyncio

    async def _seed() -> None:
        async with db_factory() as s:
            s.add_all([
                Chore(name="Vacuum", frequency_days=7, enabled=True),
                Chore(name="Mop", frequency_days=14, enabled=True),
                Chore(name="Disabled", frequency_days=1, enabled=False),
            ])
            await s.commit()
    asyncio.get_event_loop().run_until_complete(_seed())

    r = client.get("/me/chores")
    assert r.status_code == 200
    assert "Vacuum" in r.text
    assert "Mop" in r.text
    assert "Disabled" not in r.text


def test_me_chores_save_writes_deny(client: TestClient, db_factory) -> None:
    _make_member(db_factory)
    _login_member(client)
    import asyncio

    async def _seed() -> int:
        async with db_factory() as s:
            c = Chore(name="Dust", frequency_days=14, enabled=True)
            s.add(c)
            await s.commit()
            await s.refresh(c)
            return c.id
    chore_id = asyncio.get_event_loop().run_until_complete(_seed())

    # Submit empty form -> opt out of all chores.
    r = client.post("/me/chores", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/me/chores?saved=1"

    async def _check() -> dict:
        async with db_factory() as s:
            rows = (
                await s.execute(select(ChoreEligibility).where(ChoreEligibility.chore_id == chore_id))
            ).scalars().all()
            return {r.user_id: r.mode for r in rows}
    modes = asyncio.get_event_loop().run_until_complete(_check())
    assert list(modes.values()) == ["deny"]

    # Now opt back in.
    r = client.post(
        "/me/chores",
        data={"opt_in_chore_id": str(chore_id)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    modes = asyncio.get_event_loop().run_until_complete(_check())
    assert modes == {}


def test_member_can_create_chore(client: TestClient, db_factory) -> None:
    member_id = _make_member(db_factory)
    _login_member(client)
    r = client.post(
        "/me/chores/new",
        data={
            "name": "Empty bins",
            "frequency_days": "7",
            "priority": "0",
            "estimated_minutes": "5",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    after = client.get("/me/chores").text
    assert "Empty bins" in after

    import asyncio

    async def _check() -> Chore | None:
        async with db_factory() as s:
            return (
                await s.execute(select(Chore).where(Chore.name == "Empty bins"))
            ).scalar_one_or_none()
    chore = asyncio.get_event_loop().run_until_complete(_check())
    assert chore is not None
    assert chore.created_by_user_id == member_id


# -- HTTP: admin eligibility editor -------------------------------------------


def _login_admin(client: TestClient) -> None:
    r = client.post("/login", data={"name": "admin", "password": "hunter2"})
    assert r.status_code in (200, 303)


def test_admin_eligibility_editor_renders(client: TestClient, db_factory) -> None:
    _login_admin(client)
    import asyncio

    async def _seed() -> int:
        async with db_factory() as s:
            c = Chore(name="Trash", frequency_days=3, enabled=True)
            s.add_all([c, User(name="bob", active=True), User(name="carol", active=True)])
            await s.commit()
            await s.refresh(c)
            return c.id
    cid = asyncio.get_event_loop().run_until_complete(_seed())
    r = client.get(f"/admin/chores/{cid}/eligibility")
    assert r.status_code == 200
    assert "bob" in r.text
    assert "carol" in r.text
    assert "Default" in r.text


def test_admin_eligibility_editor_saves(client: TestClient, db_factory) -> None:
    _login_admin(client)
    import asyncio

    async def _seed() -> tuple[int, int, int]:
        async with db_factory() as s:
            chore = Chore(name="Recycle", frequency_days=7, enabled=True)
            bob = User(name="bob", active=True)
            carol = User(name="carol", active=True)
            s.add_all([chore, bob, carol])
            await s.commit()
            await s.refresh(chore)
            await s.refresh(bob)
            await s.refresh(carol)
            return chore.id, bob.id, carol.id
    cid, bob_id, carol_id = asyncio.get_event_loop().run_until_complete(_seed())

    r = client.post(
        f"/admin/chores/{cid}/eligibility",
        data={
            f"mode_{bob_id}": "allow",
            f"mode_{carol_id}": "deny",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    async def _check() -> dict[int, str]:
        async with db_factory() as s:
            rows = (
                await s.execute(select(ChoreEligibility).where(ChoreEligibility.chore_id == cid))
            ).scalars().all()
            return {r.user_id: r.mode for r in rows}
    modes = asyncio.get_event_loop().run_until_complete(_check())
    assert modes == {bob_id: "allow", carol_id: "deny"}
