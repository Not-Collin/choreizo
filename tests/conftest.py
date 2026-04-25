"""Test fixtures.

Spins up an in-memory SQLite engine, creates all tables, seeds an admin,
and overrides FastAPI's `get_session` dependency so the app uses the test
DB. Each test gets a fresh `TestClient` over the same app instance.
"""
from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Force a session secret + a throwaway DB path before any app modules import.
os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ.setdefault("DATABASE_PATH", "/tmp/choreizo-test-unused.db")

from app.auth import hash_password  # noqa: E402
from app.db import Base, get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402


@pytest_asyncio.fixture
async def engine() -> AsyncEngine:
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng.sync_engine, "connect")
    def _fk(dbapi, _):
        cur = dbapi.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db_factory(engine: AsyncEngine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def admin_user(db_factory) -> User:
    async with db_factory() as s:
        admin = User(name="admin", is_admin=True, active=True,
                     password_hash=hash_password("hunter2"))
        s.add(admin)
        await s.commit()
        await s.refresh(admin)
        return admin


@pytest.fixture
def client(db_factory, admin_user) -> Iterator[TestClient]:
    """TestClient with the app's get_session pointed at the test DB."""

    async def _override():
        async with db_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)
