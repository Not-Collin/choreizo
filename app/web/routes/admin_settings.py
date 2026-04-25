"""Admin: settings viewer (read-only — values come from env/config)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_admin
from app.config import get_settings
from app.models import User

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/settings", response_class=HTMLResponse)
async def settings_view(
    request: Request,
    admin: User = Depends(require_admin),
):
    s = get_settings()
    bot_configured = bool(s.telegram_bot_token)

    return templates.TemplateResponse(
        request=request,
        name="admin/settings.html",
        context={
            "current_user": admin,
            "s": s,
            "bot_configured": bot_configured,
        },
    )
