"""Integration tests for the ORM models.

Spins up an in-memory SQLite database with the same FK pragma the app uses,
creates all tables from `Base.metadata`, and exercises the relationships
and constraints we care about.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import selectinload

from app.db import Base
from app.models import (
    AppSetting,
    Assignment,
    Chore,
    ChoreEligibility,
    InviteCode,
    MagicLinkToken,
    ReminderEvent,
    User,
)


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fk(dbapi_connection, _) -> None:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


# -- Users ---------------------------------------------------------------------


async def test_create_user(session: AsyncSession) -> None:
    user = User(name="Collin", is_admin=True)
    session.add(user)
    await session.commit()
    assert user.id is not None
    assert user.active is True
    assert user.send_time == "08:00"


async def test_user_telegram_chat_id_is_unique(session: AsyncSession) -> None:
    session.add_all([User(name="A", telegram_chat_id=111), User(name="B", telegram_chat_id=111)])
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_user_send_time_format_check(session: AsyncSession) -> None:
    session.add(User(name="Bad", send_time="9am"))
    with pytest.raises(IntegrityError):
        await session.commit()


# -- Chores --------------------------------------------------------------------


async def test_create_chore_minimal(session: AsyncSession) -> None:
    chore = Chore(name="Vacuum", frequency_days=7)
    session.add(chore)
    await session.commit()
    assert chore.priority == 0
    assert chore.enabled is True


async def test_chore_frequency_must_be_positive(session: AsyncSession) -> None:
    session.add(Chore(name="Bogus", frequency_days=0))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_chore_priority_must_be_0_or_1(session: AsyncSession) -> None:
    session.add(Chore(name="Bogus", frequency_days=1, priority=2))
    with pytest.raises(IntegrityError):
        await session.commit()


# -- Eligibility ---------------------------------------------------------------


async def test_eligibility_links_user_and_chore(session: AsyncSession) -> None:
    user = User(name="Collin")
    chore = Chore(name="Vacuum", frequency_days=7)
    session.add_all([user, chore])
    await session.flush()
    session.add(ChoreEligibility(user_id=user.id, chore_id=chore.id, mode="allow"))
    await session.commit()

    fetched = (await session.execute(select(ChoreEligibility))).scalar_one()
    assert fetched.mode == "allow"
    assert fetched.user.name == "Collin"
    assert fetched.chore.name == "Vacuum"


async def test_eligibility_mode_check(session: AsyncSession) -> None:
    user = User(name="X")
    chore = Chore(name="Y", frequency_days=1)
    session.add_all([user, chore])
    await session.flush()
    session.add(ChoreEligibility(user_id=user.id, chore_id=chore.id, mode="maybe"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_eligibility_cascades_when_chore_deleted(session: AsyncSession) -> None:
    user = User(name="X")
    chore = Chore(name="Y", frequency_days=1)
    session.add_all([user, chore])
    await session.flush()
    session.add(ChoreEligibility(user_id=user.id, chore_id=chore.id, mode="allow"))
    await session.commit()

    await session.delete(chore)
    await session.commit()
    remaining = (await session.execute(select(ChoreEligibility))).scalars().all()
    assert remaining == []


# -- Assignments + reminders ---------------------------------------------------


async def test_assignment_full_lifecycle(session: AsyncSession) -> None:
    user = User(name="Collin")
    chore = Chore(name="Vacuum", frequency_days=7, priority=1)
    session.add_all([user, chore])
    await session.flush()

    a = Assignment(
        chore_id=chore.id,
        user_id=user.id,
        assigned_date="2026-04-24",
    )
    session.add(a)
    await session.flush()
    session.add(ReminderEvent(assignment_id=a.id, kind="daily_send"))
    session.add(ReminderEvent(assignment_id=a.id, kind="hourly_reminder"))
    await session.commit()

    assert a.status == "pending"
    # Re-fetch with reminders eager-loaded — async sessions can't lazy-load.
    fetched = (
        await session.execute(
            select(Assignment)
            .options(selectinload(Assignment.reminders))
            .where(Assignment.id == a.id)
        )
    ).scalar_one()
    assert len(fetched.reminders) == 2

    # cascade: deleting the assignment removes its reminders.
    await session.delete(a)
    await session.commit()
    assert (await session.execute(select(ReminderEvent))).scalars().all() == []


async def test_assignment_invalid_status_rejected(session: AsyncSession) -> None:
    user = User(name="X")
    chore = Chore(name="Y", frequency_days=1)
    session.add_all([user, chore])
    await session.flush()
    session.add(
        Assignment(
            chore_id=chore.id,
            user_id=user.id,
            assigned_date="2026-04-24",
            status="weird",
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_reminder_invalid_kind_rejected(session: AsyncSession) -> None:
    user = User(name="X")
    chore = Chore(name="Y", frequency_days=1)
    session.add_all([user, chore])
    await session.flush()
    a = Assignment(chore_id=chore.id, user_id=user.id, assigned_date="2026-04-24")
    session.add(a)
    await session.flush()
    session.add(ReminderEvent(assignment_id=a.id, kind="not-a-kind"))
    with pytest.raises(IntegrityError):
        await session.commit()


# -- Invites + magic-links -----------------------------------------------------


async def test_invite_code_round_trip(session: AsyncSession) -> None:
    invite = InviteCode(code="WELCOME01", intended_name="Partner", is_admin=False)
    session.add(invite)
    await session.commit()

    user = User(name="Partner")
    session.add(user)
    await session.flush()
    invite.used_by_user_id = user.id
    invite.used_at = datetime.now(timezone.utc)
    await session.commit()

    fetched = (await session.execute(select(InviteCode))).scalar_one()
    assert fetched.used_by_user.name == "Partner"


async def test_magic_link_token_cascades_with_user(session: AsyncSession) -> None:
    user = User(name="X")
    session.add(user)
    await session.flush()
    token = MagicLinkToken(
        token="t" * 40,
        user_id=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    session.add(token)
    await session.commit()

    await session.delete(user)
    await session.commit()
    assert (await session.execute(select(MagicLinkToken))).scalars().all() == []


# -- Settings ------------------------------------------------------------------


async def test_app_setting_kv_round_trip(session: AsyncSession) -> None:
    session.add(AppSetting(key="seed_done", value="true"))
    await session.commit()
    fetched = (await session.execute(select(AppSetting))).scalar_one()
    assert fetched.value == "true"
