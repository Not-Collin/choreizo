"""Invite-code helpers shared by the admin UI and the Telegram bot.

A code is valid when it: exists, hasn't been used, hasn't expired, and
hasn't been revoked (revoke = delete). Redemption is the only path that
populates a User's telegram_chat_id, so the same flow handles both
"existing user claims their Telegram identity" and "first-time member
arrives with intended_name set on the code".
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InviteCode, User


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def generate_code(length: int = 8) -> str:
    """Short, URL-safe, easy to type into Telegram. Defaults to 8 chars."""
    return secrets.token_urlsafe(length)[:length].upper().replace("_", "X").replace("-", "Y")


async def mint_invite(
    session: AsyncSession,
    *,
    intended_name: str | None,
    is_admin: bool,
    ttl_hours: int = 72,
) -> InviteCode:
    code = InviteCode(
        code=generate_code(),
        intended_name=(intended_name or None),
        is_admin=is_admin,
        expires_at=_now_utc() + timedelta(hours=ttl_hours),
    )
    session.add(code)
    await session.commit()
    return code


@dataclass
class RedemptionResult:
    user: User
    created: bool   # True when this redemption *created* a new User row.


class InviteError(Exception):
    pass


async def redeem_invite(
    session: AsyncSession,
    *,
    code_str: str,
    telegram_chat_id: int,
    telegram_username: str | None,
) -> RedemptionResult:
    """Apply a code: link/create a User and stamp the code as used."""
    code = (
        await session.execute(select(InviteCode).where(InviteCode.code == code_str))
    ).scalar_one_or_none()
    if code is None:
        raise InviteError("That code doesn't exist.")
    if code.used_at is not None:
        raise InviteError("That code has already been used.")
    if code.expires_at is not None:
        exp = code.expires_at if code.expires_at.tzinfo else code.expires_at.replace(tzinfo=timezone.utc)
        if exp < _now_utc():
            raise InviteError("That code has expired.")

    # Has any user already claimed this telegram_chat_id?
    existing = (
        await session.execute(
            select(User).where(User.telegram_chat_id == telegram_chat_id)
        )
    ).scalar_one_or_none()

    created = False
    if existing is not None:
        user = existing
        # An already-linked user redeeming a code with intended_name is
        # benign — we just stamp the code as used. Update telegram_username
        # in case it changed.
        if telegram_username:
            user.telegram_username = telegram_username
    else:
        user = User(
            name=(code.intended_name or telegram_username or f"member-{telegram_chat_id}"),
            telegram_chat_id=telegram_chat_id,
            telegram_username=telegram_username,
            is_admin=code.is_admin,
            active=True,
        )
        session.add(user)
        await session.flush()
        created = True

    code.used_at = _now_utc()
    code.used_by_user_id = user.id
    await session.commit()
    return RedemptionResult(user=user, created=created)
