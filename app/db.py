"""Database engine + session setup.

Defines the async SQLAlchemy engine, the session factory, and the
declarative `Base`.  SQLite-specific tweaks live here too:

  * `PRAGMA foreign_keys = ON` is set on every new connection so that
    `ondelete="CASCADE"` actually fires (SQLite has FKs off by default).
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base class for all ORM models."""


_settings = get_settings()

engine = create_async_engine(
    _settings.sqlite_async_url,
    echo=False,
    future=True,
    # aiosqlite handles thread-safety internally; check_same_thread must be off.
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine.sync_engine, "connect")
def _enable_sqlite_fk_pragma(dbapi_connection, _connection_record) -> None:
    """Turn on foreign-key enforcement for every new SQLite connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
    finally:
        cursor.close()


AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency — yields an AsyncSession scoped to one request."""
    async with AsyncSessionLocal() as session:
        yield session
