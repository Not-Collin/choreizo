"""Phase 7 tests: daily send window, callback handler, message formatting."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.notifications import (
    SEND_WINDOW_MINUTES,
    _user_send_due,
    log_daily_send,
    mark_chore_response,
    pending_assignments_for_user,
    users_needing_send,
)
from app.models import Assignment, Chore, ReminderEvent, User


# -- _user_send_due window ----------------------------------------------------


def _local(year=2026, month=4, day=24, hour=8, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute)


def test_send_due_inside_window() -> None:
    assert _user_send_due("08:00", _local(hour=8, minute=0))
    assert _user_send_due("08:00", _local(hour=8, minute=SEND_WINDOW_MINUTES - 1))


def test_send_not_due_outside_window() -> None:
    assert not _user_send_due("08:00", _local(hour=7, minute=59))
    assert not _user_send_due("08:00", _local(hour=8, minute=SEND_WINDOW_MINUTES))


def test_send_due_handles_garbage_send_time() -> None:
    assert not _user_send_due("9am", _local(hour=9))


# -- pending_assignments_for_user / users_needing_send -----------------------


@pytest.mark.asyncio
async def test_pending_assignments_filters_by_user_and_date(db_factory) -> None:
    async with db_factory() as s:
        u = User(name="u", active=True, telegram_chat_id=1)
        c1 = Chore(name="A", frequency_days=1)
        c2 = Chore(name="B", frequency_days=1)
        s.add_all([u, c1, c2])
        await s.commit()
        await s.refresh(u); await s.refresh(c1); await s.refresh(c2)
        s.add_all([
            Assignment(chore_id=c1.id, user_id=u.id, assigned_date="2026-04-24", status="pending"),
            Assignment(chore_id=c2.id, user_id=u.id, assigned_date="2026-04-23", status="pending"),
            Assignment(chore_id=c1.id, user_id=u.id, assigned_date="2026-04-24", status="completed"),
        ])
        await s.commit()

        out = await pending_assignments_for_user(s, user_id=u.id, today_str="2026-04-24")
        # Only the pending row from today.
        assert len(out) == 1
        assert out[0].chore_id == c1.id


@pytest.mark.asyncio
async def test_users_needing_send_basic(db_factory) -> None:
    async with db_factory() as s:
        u = User(name="u", active=True, telegram_chat_id=99, send_time="08:00")
        c = Chore(name="A", frequency_days=1)
        s.add_all([u, c])
        await s.commit()
        await s.refresh(u); await s.refresh(c)
        s.add(Assignment(chore_id=c.id, user_id=u.id, assigned_date="2026-04-24", status="pending"))
        await s.commit()

        # Outside window -> empty.
        early = await users_needing_send(s, now_local=_local(hour=7, minute=59))
        assert early == []

        # Inside window -> u shows up.
        out = await users_needing_send(s, now_local=_local(hour=8, minute=0))
        assert len(out) == 1
        assert out[0].user.id == u.id
        assert len(out[0].assignments) == 1


@pytest.mark.asyncio
async def test_users_needing_send_skips_already_sent(db_factory) -> None:
    async with db_factory() as s:
        u = User(name="u", active=True, telegram_chat_id=99, send_time="08:00")
        c = Chore(name="A", frequency_days=1)
        s.add_all([u, c])
        await s.commit()
        await s.refresh(u); await s.refresh(c)
        a = Assignment(chore_id=c.id, user_id=u.id, assigned_date="2026-04-24", status="pending")
        s.add(a); await s.commit(); await s.refresh(a)
        await log_daily_send(s, assignments=[a])

        out = await users_needing_send(s, now_local=_local(hour=8, minute=0))
        assert out == []


@pytest.mark.asyncio
async def test_users_needing_send_skips_unlinked_or_inactive(db_factory) -> None:
    async with db_factory() as s:
        unlinked = User(name="x", active=True, telegram_chat_id=None, send_time="08:00")
        inactive = User(name="y", active=False, telegram_chat_id=11, send_time="08:00")
        c = Chore(name="A", frequency_days=1)
        s.add_all([unlinked, inactive, c])
        await s.commit()
        await s.refresh(unlinked); await s.refresh(inactive); await s.refresh(c)
        s.add_all([
            Assignment(chore_id=c.id, user_id=unlinked.id, assigned_date="2026-04-24", status="pending"),
            Assignment(chore_id=c.id, user_id=inactive.id, assigned_date="2026-04-24", status="pending"),
        ])
        await s.commit()

        out = await users_needing_send(s, now_local=_local(hour=8, minute=0))
        assert out == []


# -- mark_chore_response -------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_done_sets_completed_at(db_factory) -> None:
    async with db_factory() as s:
        u = User(name="u", active=True, telegram_chat_id=42)
        c = Chore(name="A", frequency_days=1)
        s.add_all([u, c]); await s.commit(); await s.refresh(u); await s.refresh(c)
        a = Assignment(chore_id=c.id, user_id=u.id, assigned_date="2026-04-24", status="pending")
        s.add(a); await s.commit(); await s.refresh(a)

        result = await mark_chore_response(
            s, assignment_id=a.id, action="done", by_chat_id=42
        )
        assert result.ok
        assert result.new_status == "completed"
        await s.refresh(a)
        assert a.status == "completed"
        assert a.completed_at is not None
        assert a.responded_at is not None


@pytest.mark.asyncio
async def test_mark_skip_does_not_set_completed_at(db_factory) -> None:
    async with db_factory() as s:
        u = User(name="u", active=True, telegram_chat_id=7)
        c = Chore(name="A", frequency_days=1)
        s.add_all([u, c]); await s.commit(); await s.refresh(u); await s.refresh(c)
        a = Assignment(chore_id=c.id, user_id=u.id, assigned_date="2026-04-24", status="pending")
        s.add(a); await s.commit(); await s.refresh(a)

        result = await mark_chore_response(
            s, assignment_id=a.id, action="skip", by_chat_id=7
        )
        assert result.ok
        assert result.new_status == "skipped"
        await s.refresh(a)
        assert a.completed_at is None
        assert a.status == "skipped"


@pytest.mark.asyncio
async def test_mark_response_rejects_wrong_user(db_factory) -> None:
    async with db_factory() as s:
        owner = User(name="owner", active=True, telegram_chat_id=1)
        other = User(name="other", active=True, telegram_chat_id=2)
        c = Chore(name="A", frequency_days=1)
        s.add_all([owner, other, c]); await s.commit()
        await s.refresh(owner); await s.refresh(c)
        a = Assignment(chore_id=c.id, user_id=owner.id, assigned_date="2026-04-24", status="pending")
        s.add(a); await s.commit(); await s.refresh(a)

        result = await mark_chore_response(
            s, assignment_id=a.id, action="done", by_chat_id=2
        )
        assert not result.ok
        assert "someone else" in result.message
        await s.refresh(a)
        assert a.status == "pending"


@pytest.mark.asyncio
async def test_mark_response_idempotent_on_already_set(db_factory) -> None:
    async with db_factory() as s:
        u = User(name="u", active=True, telegram_chat_id=42)
        c = Chore(name="A", frequency_days=1)
        s.add_all([u, c]); await s.commit(); await s.refresh(u); await s.refresh(c)
        a = Assignment(
            chore_id=c.id, user_id=u.id, assigned_date="2026-04-24", status="completed",
        )
        s.add(a); await s.commit(); await s.refresh(a)

        result = await mark_chore_response(
            s, assignment_id=a.id, action="done", by_chat_id=42
        )
        # Already completed -> ok=False with explanatory message but the
        # caller should be able to re-render the resolved state.
        assert not result.ok
        assert result.new_status == "completed"


@pytest.mark.asyncio
async def test_mark_response_unknown_action(db_factory) -> None:
    async with db_factory() as s:
        u = User(name="u", active=True, telegram_chat_id=42)
        c = Chore(name="A", frequency_days=1)
        s.add_all([u, c]); await s.commit(); await s.refresh(u); await s.refresh(c)
        a = Assignment(chore_id=c.id, user_id=u.id, assigned_date="2026-04-24", status="pending")
        s.add(a); await s.commit(); await s.refresh(a)

        result = await mark_chore_response(
            s, assignment_id=a.id, action="explode", by_chat_id=42
        )
        assert not result.ok
        assert "Unknown action" in result.message


# -- TG callback parsing + format helpers -------------------------------------


def test_callback_round_trip() -> None:
    from app.tg.notify import callback_for, parse_callback

    payload = callback_for(42, "done")
    assert payload == "assign:42:done"
    assert parse_callback(payload) == (42, "done")
    assert parse_callback("not-an-assign:1:done") is None
    assert parse_callback("assign:bad:done") is None


def test_chore_message_text_includes_metadata() -> None:
    from app.tg.notify import chore_message_text

    chore = MagicMock()
    chore.name = "Vacuum"
    chore.description = "Living room rug"
    chore.frequency_days = 7
    chore.estimated_minutes = 20
    chore.priority = 1
    a = MagicMock()
    a.chore = chore
    text = chore_message_text(a)
    assert "Vacuum" in text
    assert "every 1 week" in text
    assert "~20 min" in text
    assert "high priority" in text
    assert "Living room rug" in text


# -- TG handler with mocked Bot -----------------------------------------------


@pytest.mark.asyncio
async def test_tick_sends_messages_and_logs(db_factory, monkeypatch) -> None:
    """End-to-end-ish: replace AsyncSessionLocal in tg.notify with the test
    factory, make a fake Bot, run tick, verify the Bot was called and that
    daily_send rows were written."""
    import app.tg.notify as notify_mod

    monkeypatch.setattr(notify_mod, "AsyncSessionLocal", db_factory)

    async with db_factory() as s:
        u = User(name="u", active=True, telegram_chat_id=99, send_time="08:00")
        c = Chore(name="Vacuum", frequency_days=1)
        s.add_all([u, c]); await s.commit()
        await s.refresh(u); await s.refresh(c)
        s.add(Assignment(chore_id=c.id, user_id=u.id, assigned_date="2026-04-24", status="pending"))
        await s.commit()

    bot = MagicMock()
    bot.send_message = AsyncMock()

    sent = await notify_mod.tick(bot, now_local=_local(hour=8, minute=0))
    assert sent == 1
    # 1 intro + 1 per chore.
    assert bot.send_message.call_count == 2

    # daily_send row should now exist.
    async with db_factory() as s:
        rows = (
            await s.execute(
                select(ReminderEvent).where(ReminderEvent.kind == "daily_send")
            )
        ).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_handle_chore_callback_round_trip(db_factory, monkeypatch) -> None:
    import app.tg.bot as bot_mod
    monkeypatch.setattr(bot_mod, "AsyncSessionLocal", db_factory)

    async with db_factory() as s:
        u = User(name="u", active=True, telegram_chat_id=42)
        c = Chore(name="A", frequency_days=1)
        s.add_all([u, c]); await s.commit(); await s.refresh(u); await s.refresh(c)
        a = Assignment(chore_id=c.id, user_id=u.id, assigned_date="2026-04-24", status="pending")
        s.add(a); await s.commit(); await s.refresh(a)
        aid = a.id

    ok, msg, new_status, returned_id = await bot_mod.handle_chore_callback(
        callback_data=f"assign:{aid}:done", by_chat_id=42,
    )
    assert ok
    assert new_status == "completed"
    assert returned_id == aid

    async with db_factory() as s:
        a = (await s.execute(select(Assignment).where(Assignment.id == aid))).scalar_one()
        assert a.status == "completed"


# -- Snooze -------------------------------------------------------------------


def test_snooze_callback_round_trip() -> None:
    from app.tg.notify import parse_snooze_callback, snooze_callback_for

    payload = snooze_callback_for(7, 2)
    assert payload == "snooze:7:2"
    assert parse_snooze_callback(payload) == (7, 2)
    assert parse_snooze_callback("assign:7:done") is None
    assert parse_snooze_callback("snooze:bad:2") is None


@pytest.mark.asyncio
async def test_snooze_assignment_sets_snoozed_until(db_factory) -> None:
    from app.notifications import snooze_assignment

    async with db_factory() as s:
        u = User(name="u", active=True, telegram_chat_id=55)
        c = Chore(name="A", frequency_days=1)
        s.add_all([u, c]); await s.commit(); await s.refresh(u); await s.refresh(c)
        a = Assignment(chore_id=c.id, user_id=u.id, assigned_date="2026-04-24", status="pending")
        s.add(a); await s.commit(); await s.refresh(a)
        aid = a.id

        result = await snooze_assignment(s, assignment_id=aid, by_chat_id=55, hours=2)
        assert result.ok
        assert "2 hours" in result.message

        await s.refresh(a)
        assert a.snoozed_until is not None
        assert a.status == "pending"  # status unchanged — it's still pending


@pytest.mark.asyncio
async def test_snooze_rejects_wrong_user(db_factory) -> None:
    from app.notifications import snooze_assignment

    async with db_factory() as s:
        owner = User(name="owner", active=True, telegram_chat_id=1)
        c = Chore(name="A", frequency_days=1)
        s.add_all([owner, c]); await s.commit(); await s.refresh(owner); await s.refresh(c)
        a = Assignment(chore_id=c.id, user_id=owner.id, assigned_date="2026-04-24", status="pending")
        s.add(a); await s.commit(); await s.refresh(a)

        result = await snooze_assignment(s, assignment_id=a.id, by_chat_id=999, hours=1)
        assert not result.ok


@pytest.mark.asyncio
async def test_daily_send_skips_snoozed_assignments(db_factory) -> None:
    """A user whose only assignment is snoozed should not get a daily send."""
    from datetime import datetime, timezone

    async with db_factory() as s:
        u = User(name="u", active=True, telegram_chat_id=99, send_time="08:00")
        c = Chore(name="A", frequency_days=1)
        s.add_all([u, c]); await s.commit(); await s.refresh(u); await s.refresh(c)
        # Snoozed far into the future so it's always active regardless of wall-clock time
        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        a = Assignment(
            chore_id=c.id, user_id=u.id, assigned_date="2026-04-24",
            status="pending", snoozed_until=future,
        )
        s.add(a); await s.commit()

        batches = await users_needing_send(s, now_local=_local(hour=8, minute=0))
        assert batches == []


# -- Member settings ----------------------------------------------------------


def test_member_settings_page_renders(client) -> None:
    client.post("/login", data={"name": "admin", "password": "hunter2"})
    r = client.get("/me/settings")
    assert r.status_code == 200
    assert "send time" in r.text.lower()


def test_member_settings_save_updates_send_time(client) -> None:
    client.post("/login", data={"name": "admin", "password": "hunter2"})
    r = client.post(
        "/me/settings",
        data={"send_time": "09:30"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    r2 = client.get("/me/settings")
    assert "09:30" in r2.text


def test_member_settings_rejects_invalid_time(client) -> None:
    client.post("/login", data={"name": "admin", "password": "hunter2"})
    r = client.post("/me/settings", data={"send_time": "9am"})
    assert r.status_code == 400
    assert "HH:MM" in r.text


# -- HTTP: admin /admin/send-now -----------------------------------------------


def test_admin_send_now(client: TestClient, monkeypatch) -> None:
    """POST /admin/send-now triggers our injected callable."""
    import app.web.routes.admin_assignments as mod

    called = {"n": 0}
    async def fake_send_now() -> int:
        called["n"] += 1
        return 3
    monkeypatch.setattr(mod, "_send_now", fake_send_now)

    r = client.post("/login", data={"name": "admin", "password": "hunter2"})
    assert r.status_code in (200, 303)
    r = client.post("/admin/send-now", follow_redirects=False)
    assert r.status_code == 303
    assert "sent=3" in r.headers["location"]
    assert called["n"] == 1
