"""Member-facing routes (mounted under /me)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_current_user
from app.config import get_effective_str, get_settings
from app.db import get_session
from app.eligibility import get_eligibility_map, set_member_optin
from app.models import Assignment, Chore, User
from app.web.routes.admin import _UNIT_DAYS

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

router = APIRouter(prefix="/me", tags=["member"])

_ACTION_TO_STATUS = {"done": "completed", "skip": "skipped", "ignore": "ignored"}


def _require_member(user: User | None) -> User:
    if user is None or not user.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


def _tz() -> ZoneInfo:
    return ZoneInfo(get_effective_str("house_timezone", get_settings().house_timezone))


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def me_home(
    request: Request,
    user: User | None = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    me = _require_member(user)
    tz = _tz()
    today = datetime.now(tz).date()
    today_str = today.isoformat()

    today_assignments = (
        await session.execute(
            select(Assignment)
            .where(Assignment.user_id == me.id, Assignment.assigned_date == today_str)
            .options(selectinload(Assignment.chore))
            .order_by(Assignment.id)
        )
    ).scalars().all()

    # Recent history — last 10 completed/skipped/ignored assignments (not today)
    recent = (
        await session.execute(
            select(Assignment)
            .where(
                Assignment.user_id == me.id,
                Assignment.assigned_date < today_str,
                Assignment.status.in_(["completed", "skipped", "ignored"]),
            )
            .options(selectinload(Assignment.chore))
            .order_by(Assignment.assigned_date.desc(), Assignment.id.desc())
            .limit(10)
        )
    ).scalars().all()

    # Upcoming chores — due within 7 days
    upcoming = await _upcoming_chores(session, me, today)

    # Quick stats — this calendar month
    month_start = today.replace(day=1).isoformat()
    done_count = (
        await session.execute(
            select(func.count()).where(
                Assignment.user_id == me.id,
                Assignment.assigned_date >= month_start,
                Assignment.status == "completed",
            )
        )
    ).scalar_one()
    skipped_count = (
        await session.execute(
            select(func.count()).where(
                Assignment.user_id == me.id,
                Assignment.assigned_date >= month_start,
                Assignment.status == "skipped",
            )
        )
    ).scalar_one()

    return templates.TemplateResponse(
        request=request,
        name="me.html",
        context={
            "current_user": me,
            "today": today_str,
            "today_assignments": today_assignments,
            "recent": recent,
            "upcoming": upcoming,
            "done_count": done_count,
            "skipped_count": skipped_count,
        },
    )


async def _upcoming_chores(
    session: AsyncSession,
    me: User,
    today: date,
    horizon_days: int = 7,
) -> list[dict]:
    """Return chores likely due in the next horizon_days, sorted by expected date."""
    horizon = today + timedelta(days=horizon_days)

    chores = (
        await session.execute(
            select(Chore).where(Chore.enabled.is_(True)).order_by(Chore.name)
        )
    ).scalars().all()

    # Latest assignment date per chore for this user
    latest_rows = (
        await session.execute(
            select(Assignment.chore_id, func.max(Assignment.assigned_date))
            .where(Assignment.user_id == me.id)
            .group_by(Assignment.chore_id)
        )
    ).all()
    latest_by_chore: dict[int, str | None] = {chore_id: last for chore_id, last in latest_rows}

    upcoming = []
    for chore in chores:
        if chore.next_due_date:
            next_due = date.fromisoformat(chore.next_due_date)
        else:
            last = latest_by_chore.get(chore.id)
            if last:
                next_due = date.fromisoformat(last) + timedelta(days=chore.frequency_days)
            else:
                next_due = today

        if next_due > horizon:
            continue
        if next_due < today:
            next_due = today

        # Month filter: advance next_due into the first allowed month if needed
        if chore.allowed_months:
            allowed_m = {int(m) for m in chore.allowed_months.split(",") if m.strip().isdigit()}
            # Walk day by day until we hit an allowed month or exceed horizon
            candidate = next_due
            while candidate.month not in allowed_m:
                # Jump to first day of next month
                if candidate.month == 12:
                    candidate = candidate.replace(year=candidate.year + 1, month=1, day=1)
                else:
                    candidate = candidate.replace(month=candidate.month + 1, day=1)
                if candidate > horizon:
                    break
            if candidate > horizon:
                continue
            next_due = candidate

        # Advance to the nearest allowed weekday within the window
        if chore.allowed_weekdays:
            allowed_w = {int(d) for d in chore.allowed_weekdays.split(",") if d.strip().isdigit()}
            candidate = next_due
            for _ in range(horizon_days + 1):
                if candidate.weekday() in allowed_w:
                    next_due = candidate
                    break
                candidate += timedelta(days=1)
            else:
                continue  # no allowed day in window

        upcoming.append({"chore": chore, "next_due": next_due.isoformat()})

    upcoming.sort(key=lambda x: x["next_due"])
    return upcoming


@router.post("/assignments/{assignment_id}/{action}")
async def assignment_action(
    assignment_id: int,
    action: str,
    user: User | None = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    me = _require_member(user)
    if action not in _ACTION_TO_STATUS:
        raise HTTPException(status_code=400, detail="Invalid action")
    a = await session.get(Assignment, assignment_id)
    if a is None or a.user_id != me.id:
        raise HTTPException(status_code=404)
    if a.status in ("pending", "overdue"):
        now = datetime.now(timezone.utc)
        a.status = _ACTION_TO_STATUS[action]
        a.responded_at = now
        if action == "done":
            a.completed_at = now
        await session.commit()
    return RedirectResponse("/me", status_code=303)


@dataclass
class _ChoreRow:
    chore: Chore
    eligible: bool
    locked: bool   # True when the chore's eligibility is admin-managed (allow-list)
    next_due: str | None = None  # ISO date when this chore is next due, or None if unknown


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
    today = date.today()

    chores = (
        await session.execute(select(Chore).where(Chore.enabled.is_(True)).order_by(Chore.name))
    ).scalars().all()

    # Global latest assignment date per chore (any user) — used to project next due.
    latest_rows = (
        await session.execute(
            select(Assignment.chore_id, func.max(Assignment.assigned_date))
            .group_by(Assignment.chore_id)
        )
    ).all()
    latest_by_chore: dict[int, str] = {cid: last for cid, last in latest_rows if last}

    rows: list[_ChoreRow] = []
    any_locked = False
    for c in chores:
        eligibility = await get_eligibility_map(session, c)
        any_allow = any(m == "allow" for m in eligibility.values())
        my_mode = eligibility.get(me.id)

        if any_allow:
            eligible = my_mode == "allow"
            locked = True
        else:
            eligible = my_mode != "deny"
            locked = False
        if locked:
            any_locked = True

        # Compute next due date for display.
        if c.next_due_date:
            next_due: str | None = c.next_due_date
        else:
            last = latest_by_chore.get(c.id)
            if last:
                projected = date.fromisoformat(last) + timedelta(days=c.frequency_days)
                next_due = max(projected, today).isoformat()
            else:
                next_due = None  # never assigned yet

        rows.append(_ChoreRow(chore=c, eligible=eligible, locked=locked, next_due=next_due))
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
    frequency_amount: int = Form(1),
    frequency_unit: str = Form("week"),
    priority: int = Form(0),
    estimated_minutes: str | None = Form(None),
    next_due_date: str | None = Form(None),
    user: User | None = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    me = _require_member(user)
    form = await request.form()
    weekday_values = list(form.getlist("allowed_weekday"))
    month_values = list(form.getlist("allowed_month"))
    frequency_days = max(1, frequency_amount * _UNIT_DAYS.get(frequency_unit, 1))
    if priority not in (0, 1):
        return templates.TemplateResponse(
            request=request,
            name="me_chore_new.html",
            context={
                "current_user": me,
                "form": form,
                "error": "Priority must be normal or high.",
            },
            status_code=400,
        )
    days = sorted({int(d) for d in weekday_values if d.isdigit() and 0 <= int(d) <= 6})
    allowed_weekdays = None if len(days) == 0 or len(days) == 7 else ",".join(str(d) for d in days)
    months = sorted({int(m) for m in month_values if m.isdigit() and 1 <= int(m) <= 12})
    allowed_months = None if len(months) == 0 or len(months) == 12 else ",".join(str(m) for m in months)
    ndd = (next_due_date or "").strip() or None
    chore = Chore(
        name=name.strip(),
        description=(description or "").strip() or None,
        frequency_days=frequency_days,
        priority=priority,
        estimated_minutes=int(estimated_minutes) if estimated_minutes else None,
        allowed_weekdays=allowed_weekdays,
        allowed_months=allowed_months,
        next_due_date=ndd,
        enabled=True,
        created_by_user_id=me.id,
    )
    session.add(chore)
    await session.commit()
    return RedirectResponse("/me/chores?saved=1", status_code=303)


# ---------------------------------------------------------------------------
# Member settings (send time, etc.)
# ---------------------------------------------------------------------------

_SEND_TIME_RE = __import__("re").compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@router.get("/settings", response_class=HTMLResponse)
async def my_settings(
    request: Request,
    saved: int = 0,
    user: User | None = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    me = _require_member(user)
    return templates.TemplateResponse(
        request=request,
        name="me_settings.html",
        context={
            "current_user": me,
            "saved": bool(saved),
        },
    )


@router.post("/settings")
async def my_settings_save(
    request: Request,
    send_time: str = Form("08:00"),
    user: User | None = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    me = _require_member(user)
    send_time = send_time.strip()
    if not _SEND_TIME_RE.match(send_time):
        return templates.TemplateResponse(
            request=request,
            name="me_settings.html",
            context={
                "current_user": me,
                "saved": False,
                "error": "Send time must be in HH:MM format (e.g. 08:00).",
            },
            status_code=400,
        )
    # Re-fetch inside this session so the update commits cleanly.
    db_me = await session.get(User, me.id)
    if db_me is not None:
        db_me.send_time = send_time
        await session.commit()
    return RedirectResponse("/me/settings?saved=1", status_code=303)
