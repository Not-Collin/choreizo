"""Authentication helpers.

Password hashing uses bcrypt directly (passlib is heavy and unmaintained as
of 2025).  Sessions are Starlette signed cookies — see main.py for the
SessionMiddleware wiring.
"""
from __future__ import annotations

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User


def hash_password(plain: str) -> str:
    """Return a bcrypt hash for `plain` using a fresh per-call salt."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time bcrypt compare. Tolerates bad/empty hashes."""
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User | None:
    """Look up the user whose id is recorded in the signed session cookie."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return await session.get(User, user_id)


async def require_admin(
    user: User | None = Depends(get_current_user),
) -> User:
    """Dependency that 401s when no session, 403s when session isn't admin."""
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    if not user.is_admin or not user.active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return user
