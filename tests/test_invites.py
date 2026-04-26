"""Phase 4 tests: invite codes, magic links, /start handler."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.invites import InviteError, mint_invite, redeem_invite
from app.magic import mint_magic_link, consume_magic_link
from app.models import InviteCode, MagicLinkToken, User


def _login_admin(client: TestClient) -> None:
    r = client.post("/login", data={"name": "admin", "password": "hunter2"})
    assert r.status_code in (200, 303)


# -- Admin invite UI ----------------------------------------------------------


def test_admin_can_mint_invite(client: TestClient) -> None:
    _login_admin(client)
    r = client.post(
        "/admin/invites",
        data={"intended_name": "Partner", "ttl_hours": "48"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/admin/invites?just_minted=" in r.headers["location"]

    listing = client.get("/admin/invites").text
    assert "Partner" in listing
    assert "active" in listing


def test_revoking_unused_invite_removes_it(client: TestClient) -> None:
    _login_admin(client)
    client.post("/admin/invites", data={"intended_name": "X", "ttl_hours": "1"})
    listing = client.get("/admin/invites").text
    # Pull the code out of the listing (it appears inside <code>...</code>).
    import re
    m = re.search(r"<td><code>([A-Z0-9]+)</code></td>", listing)
    assert m, listing
    code = m.group(1)

    r = client.post(f"/admin/invites/{code}/revoke", follow_redirects=False)
    assert r.status_code == 303
    after = client.get("/admin/invites").text
    assert code not in after


# -- Invite redemption (pure DB logic) ----------------------------------------


@pytest.mark.asyncio
async def test_redeem_creates_user_when_telegram_unknown(db_factory) -> None:
    async with db_factory() as s:
        code = await mint_invite(s, intended_name="Partner", is_admin=False, ttl_hours=1)
    async with db_factory() as s:
        result = await redeem_invite(
            s, code_str=code.code, telegram_chat_id=12345, telegram_username="part"
        )
        assert result.created is True
        assert result.user.name == "Partner"
        assert result.user.telegram_chat_id == 12345

        used = (await s.execute(select(InviteCode).where(InviteCode.code == code.code))).scalar_one()
        assert used.used_at is not None
        assert used.used_by_user_id == result.user.id


@pytest.mark.asyncio
async def test_redeem_links_existing_user_when_chat_id_known(db_factory) -> None:
    async with db_factory() as s:
        existing = User(name="Already", telegram_chat_id=555)
        s.add(existing)
        await s.commit()
        await s.refresh(existing)
        code = await mint_invite(s, intended_name="Partner", is_admin=False, ttl_hours=1)
    async with db_factory() as s:
        result = await redeem_invite(
            s, code_str=code.code, telegram_chat_id=555, telegram_username="al"
        )
        assert result.created is False
        assert result.user.id == existing.id


@pytest.mark.asyncio
async def test_redeem_rejects_used_code(db_factory) -> None:
    async with db_factory() as s:
        code = await mint_invite(s, intended_name="A", is_admin=False, ttl_hours=1)
    async with db_factory() as s:
        await redeem_invite(s, code_str=code.code, telegram_chat_id=1, telegram_username=None)
    async with db_factory() as s:
        with pytest.raises(InviteError, match="already been used"):
            await redeem_invite(
                s, code_str=code.code, telegram_chat_id=2, telegram_username=None
            )


@pytest.mark.asyncio
async def test_redeem_rejects_expired_code(db_factory) -> None:
    async with db_factory() as s:
        code = InviteCode(
            code="EXPIRED1",
            intended_name="A",
            is_admin=False,
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        s.add(code)
        await s.commit()
    async with db_factory() as s:
        with pytest.raises(InviteError, match="expired"):
            await redeem_invite(
                s, code_str="EXPIRED1", telegram_chat_id=1, telegram_username=None
            )


@pytest.mark.asyncio
async def test_redeem_rejects_unknown_code(db_factory) -> None:
    async with db_factory() as s:
        with pytest.raises(InviteError, match="doesn't exist"):
            await redeem_invite(
                s, code_str="NOPE", telegram_chat_id=1, telegram_username=None
            )


# -- Magic links --------------------------------------------------------------


@pytest.mark.asyncio
async def test_magic_link_round_trip(db_factory) -> None:
    async with db_factory() as s:
        u = User(name="Member", active=True)
        s.add(u)
        await s.commit()
        await s.refresh(u)
        url = await mint_magic_link(s, u)
        assert "/auth/magic/" in url

    token = url.rsplit("/", 1)[-1]
    async with db_factory() as s:
        consumed = await consume_magic_link(s, token)
        assert consumed is not None
        assert consumed.id == u.id
    async with db_factory() as s:
        # Second use fails because used_at is set.
        again = await consume_magic_link(s, token)
        assert again is None


@pytest.mark.asyncio
async def test_magic_link_expired_returns_none(db_factory) -> None:
    async with db_factory() as s:
        u = User(name="Member", active=True)
        s.add(u)
        await s.commit()
        await s.refresh(u)
        token = "expired-token-xxx"
        s.add(MagicLinkToken(
            token=token,
            user_id=u.id,
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        ))
        await s.commit()
    async with db_factory() as s:
        assert await consume_magic_link(s, token) is None


# -- HTTP /auth/magic/{token} -------------------------------------------------


def test_magic_link_endpoint_logs_user_in(client: TestClient, db_factory) -> None:
    """Hit /auth/magic/{token} with a real token created in the test DB."""
    import asyncio

    async def _setup() -> str:
        async with db_factory() as s:
            u = User(name="Member", active=True, is_admin=False)
            s.add(u)
            await s.commit()
            await s.refresh(u)
            url = await mint_magic_link(s, u)
            return url.rsplit("/", 1)[-1]

    token = asyncio.get_event_loop().run_until_complete(_setup())

    r = client.get(f"/auth/magic/{token}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/me"

    me = client.get("/me")
    assert me.status_code == 200
    assert "Member" in me.text


def test_magic_link_invalid_token_shows_error(client: TestClient) -> None:
    r = client.get("/auth/magic/not-a-real-token", follow_redirects=False)
    assert r.status_code == 400
    assert "invalid or expired" in r.text


# -- /start handler -----------------------------------------------------------


@pytest.mark.asyncio
async def test_start_handler_happy_path(db_factory, monkeypatch) -> None:
    """redeem_start uses AsyncSessionLocal — point it at the test DB."""
    import app.tg.bot as bot_mod

    monkeypatch.setattr(bot_mod, "AsyncSessionLocal", db_factory)

    async with db_factory() as s:
        code = await mint_invite(s, intended_name="Roomie", is_admin=False, ttl_hours=1)

    ok, msg = await bot_mod.redeem_start(
        code.code, chat_id=99887766, username="roomie"
    )
    assert ok is True
    assert "Roomie" in msg
    assert "/auth/magic/" in msg


@pytest.mark.asyncio
async def test_start_handler_rejects_bad_code(db_factory, monkeypatch) -> None:
    import app.tg.bot as bot_mod

    monkeypatch.setattr(bot_mod, "AsyncSessionLocal", db_factory)
    ok, msg = await bot_mod.redeem_start(
        "WRONGCODE", chat_id=1, username=None
    )
    assert ok is False
    assert "doesn't exist" in msg


@pytest.mark.asyncio
async def test_start_handler_no_args(db_factory, monkeypatch) -> None:
    import app.tg.bot as bot_mod

    monkeypatch.setattr(bot_mod, "AsyncSessionLocal", db_factory)
    ok, msg = await bot_mod.redeem_start("", chat_id=1, username=None)
    assert ok is False
    assert "Send /start" in msg
