"""Login / logout / magic-link routes."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, verify_password
from app.db import get_session
from app.magic import consume_magic_link
from app.models import User

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

router = APIRouter(tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
async def login_form(
    request: Request,
    user: User | None = Depends(get_current_user),
):
    if user is not None:
        return RedirectResponse("/admin" if user.is_admin else "/me", status_code=303)
    return templates.TemplateResponse(
        request=request, name="login.html", context={"current_user": None}
    )


@router.post("/login")
async def login_submit(
    request: Request,
    name: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(User).where(User.name == name))
    user = result.scalar_one_or_none()
    if user is None or not user.active or not verify_password(password, user.password_hash or ""):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"current_user": None, "name": name, "error": "Invalid credentials."},
            status_code=401,
        )
    request.session["user_id"] = user.id
    return RedirectResponse("/admin" if user.is_admin else "/me", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/auth/magic/{token}")
async def magic_consume(
    token: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """One-shot login from a Telegram-DM'd link.

    Successful consume sets the session and redirects admins to /admin
    and members to /me. Invalid/expired tokens get a polite login page.
    """
    user = await consume_magic_link(session, token)
    if user is None:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "current_user": None,
                "error": "That link is invalid or expired. Ask for a new one in Telegram.",
            },
            status_code=400,
        )
    request.session["user_id"] = user.id
    return RedirectResponse("/admin" if user.is_admin else "/me", status_code=303)
