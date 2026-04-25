"""Short-lived magic-link tokens for member auth.

Members never type a password — they sign in by clicking a link the
Telegram bot DMs them. Each link is a one-shot, time-bounded token row in
`magic_link_tokens`. The token itself is a URL-safe random string (not a
JWT) so consumption is a single SQL update with no signature math.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import MagicLinkToken, User

DEFAULT_TTL = timedelta(minutes=15)


async def mint_magic_link(
    session: AsyncSession,
    user: User,
    *,
    ttl: timedelta | None = None,
) -> str:
    """Create a token row and return the absolute URL the user should open."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + (ttl or DEFAULT_TTL)
    session.add(MagicLinkToken(token=token, user_id=user.id, expires_at=expires))
    await session.commit()
    base = get_settings().base_url.rstrip("/")
    return f"{base}/auth/magic/{token}"


async def consume_magic_link(
    session: AsyncSession,
    token: str,
) -> User | None:
    """Mark the token used and return the bound user, or None if unusable.

    Returns None when the token is missing, already used, expired, or the
    bound user is inactive.
    """
    row = (
        await session.execute(
            select(MagicLinkToken).where(MagicLinkToken.token == token)
        )
    ).scalar_one_or_none()
    if row is None or row.used_at is not None:
        return None
    now = datetime.now(timezone.utc)
    # SQLite returns naive datetimes by default — coerce to aware UTC for compare.
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires < now:
        return None
    user = await session.get(User, row.user_id)
    if user is None or not user.active:
        return None
    row.used_at = now
    await session.commit()
    return user
