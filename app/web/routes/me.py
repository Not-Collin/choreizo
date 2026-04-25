"""Member-facing routes (mounted under /me)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_session
from app.eligibility import get_eligibility_map, set_member_optin
from app.models import Chore, User

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

router = APIRouter(prefix="/me", tags=["member"])


def _require_member(user: User | None) -> User:
    if user is None or not user.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def me_home(request: Request, user: User | None = Depends(get_current_user)):
    me = _require_member(user)
    return templates.TemplateResponse(
        request=request, name="me.html", context={"current_user": me}
    )


@dataclass
class _ChoreRow:
    chore: Chore
    eligible: bool
    locked: bool   # True when the chore's eligibility is admin-managed (allow-list)


async def _build_chore_rows(
    session: AsyncSession,
    me: User,
) -> tuple[list[_ChoreRow], bool]:
    """Return (rows, any_locked).

    A chore is "locked" for a member when it's in admin-allow-list mode
    (at least one allow row exists) and the member isn't on that list.
    Members can't escape an allow-list by ticking their own checkbox, so
    those rows render disabled.
    """
    chores = (
        await session.execute(select(Chore).where(Chore.enabled.is_(True)).order_by(Chore.name))
    ).scalars().all()

    rows: list[_ChoreRow] = []
    any_locked = False
    for c in chores:
        eligibility = await get_eligibility_map(session, c)
        any_allow = any(m == "allow" for m in eligibility.values())
        my_mode = eligibility.get(me.id)

        if any_allow:
            # Allow-list mode — admin owns membership.
            eligible = my_mode == "allow"
            locked = my_mode != "allow"  # Member can flip OFF their own allow? No, locked.
            # Actually: the member should be able to opt out even from an allow
            # list. But Phase 5 keeps members read-only when in allow-list mode
            # to match the docstring. The admin editor is the escape hatch.
            locked = True
        else:
            eligible = my_mode != "deny"
            locked = False
        if locked:
            any_locked = True
        rows.append(_ChoreRow(chore=c, eligible=eligible, locked=locked))
    return rows, any_locked


@router.get("/chores", response_class=HTMLResponse)
async def my_chores(
    request: Request,
    saved: int = 0,
    user: User | None = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    me = _require_member(user)
    rows, any_locked = await _build_chore_rows(session, me)
    return templates.TemplateResponse(
        request=request,
        name="me_chores.html",
        context={
            "current_user": me,
            "rows": rows,
            "saved": bool(saved),
            "locked_out": any_locked,
        },
    )


@router.post("/chores")
async def my_chores_save(
    request: Request,
    user: User | None = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Receive the checkbox set and reconcile deny rows.

    Form posts only the *checked* checkboxes' values, so the unchecked
    ones are inferred from "all enabled, non-locked chores not in the
    submitted set".
    """
    me = _require_member(user)
    form = await request.form()
    opted_in_ids = {int(v) for v in form.getlist("opt_in_chore_id")}

    rows, _ = await _build_chore_rows(session, me)
    for row in rows:
        if row.locked:
            continue
        wants_in = row.chore.id in opted_in_ids
        if wants_in == row.eligible:
            continue  # no change
        await set_member_optin(session, chore=row.chore, user=me, opted_in=wants_in)
    return RedirectResponse("/me/chores?saved=1", status_code=303)


@router.get("/chores/new", response_class=HTMLResponse)
async def my_chore_new(
    request: Request,
    user: User | None = Depends(get_current_user),
):
    me = _require_member(user)
    return templates.TemplateResponse(
        request=request,
        name="me_chore_new.html",
        context={"current_user": me, "form": {}},
    )


@router.post("/chores/new")
async def my_chore_create(
    request: Request,
    name: str = Form(...),
    description: str | None = Form(None),
    frequency_days: int = Form(...),
    priority: int = Form(0),
    estimated_minutes: str | None = Form(None),
    user: User | None = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    me = _require_member(user)
    if frequency_days < 1 or priority not in (0, 1):
        return templates.TemplateResponse(
            request=request,
            name="me_chore_new.html",
            context={
                "current_user": me,
                "form": await request.form(),
                "error": "Frequency must be ≥ 1 day and priority must be normal or high.",
            },
            status_code=400,
        )
    chore = Chore(
        name=name.strip(),
        description=(description or "").strip() or None,
        frequency_days=frequency_days,
        priority=priority,
        estimated_minutes=int(estimated_minutes) if estimated_minutes else None,
        enabled=True,
        created_by_user_id=me.id,
    )
    session.add(chore)
    await session.commit()
    return RedirectResponse("/me/chores?saved=1", status_code=303)
