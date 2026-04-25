"""Application entry point."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from app import __version__
from app.auth import hash_password
from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models import User

log = logging.getLogger("choreizo")

_TEMPLATE_DIR = Path(__file__).parent / "web" / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


async def _bootstrap_admin() -> None:
    settings = get_settings()
    if not settings.admin_password:
        return
    async with AsyncSessionLocal() as session:
        existing = await session.execute(
            select(User).where(User.is_admin.is_(True)).limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            return
        admin = User(
            name="admin", is_admin=True, active=True,
            password_hash=hash_password(settings.admin_password),
        )
        session.add(admin)
        await session.commit()
        log.info("Bootstrapped admin user id=%s", admin.id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Choreizo %s starting up", __version__)
    await _bootstrap_admin()
    from app.web.routes.admin_settings import load_db_overrides
    async with AsyncSessionLocal() as _s:
        await load_db_overrides(_s)
    from app.scheduler import build_scheduler, clear_active_bot, set_active_bot
    from app.tg.bot import build_application, start_polling, stop_polling

    tg_app = build_application()
    if tg_app is not None:
        await start_polling(tg_app)
        set_active_bot(tg_app.bot)
    sched = build_scheduler()
    sched.start()
    log.info("Scheduler started")
    try:
        yield
    finally:
        sched.shutdown(wait=False)
        clear_active_bot()
        if tg_app is not None:
            await stop_polling(tg_app)
        log.info("Choreizo shutting down")


app = FastAPI(
    title="Choreizo", version=__version__,
    description="Self-hosted random daily chore selector and tracker.",
    lifespan=lifespan,
)

_settings = get_settings()
app.add_middleware(
    SessionMiddleware,
    secret_key=_settings.session_secret or "dev-insecure-change-me",
    session_cookie="choreizo_session",
    same_site="lax", https_only=False,
    max_age=60 * 60 * 24 * 30,
)

# Routers
from app.web.routes import admin as admin_routes
from app.web.routes import admin_assignments as admin_assignments_routes
from app.web.routes import admin_eligibility as admin_elig_routes
from app.web.routes import admin_reports as admin_reports_routes
from app.web.routes import admin_settings as admin_settings_routes
from app.web.routes import auth as auth_routes
from app.web.routes import invites as invites_routes
from app.web.routes import me as me_routes

app.include_router(auth_routes.router)
app.include_router(admin_routes.router)
app.include_router(admin_elig_routes.router)
app.include_router(admin_assignments_routes.router)
app.include_router(admin_reports_routes.router)
app.include_router(admin_settings_routes.router)
app.include_router(invites_routes.router)
app.include_router(me_routes.router)


@app.exception_handler(HTTPException)
async def _redirect_unauthenticated(request: Request, exc: HTTPException):
    accept = request.headers.get("accept", "")
    if exc.status_code == 401 and "text/html" in accept:
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "choreizo", "version": __version__}


@app.get("/", response_class=HTMLResponse, tags=["web"])
async def index(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/admin", status_code=303)
    return RedirectResponse("/login", status_code=303)


async def _serve() -> None:
    settings = get_settings()
    config = uvicorn.Config(
        app, host=settings.host, port=settings.port,
        log_level=settings.log_level.lower(), access_log=True,
    )
    server = uvicorn.Server(config)
    await server.serve()


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
