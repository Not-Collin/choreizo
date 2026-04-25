"""Telegram bot — long-polling Application started in FastAPI's lifespan.

Handlers registered here:
  /start <code>   — claim an invite code and link this Telegram account.
  /today          — resend today's pending chore assignments.
  /done  <id>     — mark an assignment completed.
  /skip  <id>     — mark an assignment skipped (retries next cycle).
  /ignore <id>    — mark an assignment ignored (closes the reminder loop).
  /help           — list available commands.
  /whoami         — show the linked identity for this Telegram account.
  /web            — DM a magic-link URL to log in to the member web UI.
  /admin          — DM a magic-link URL for admin web UI (admins only).
  callback "assign" — Done/Skip/Ignore from inline buttons (all phases).

Daily push DMs come from app.tg.notify.tick, called by the per-minute
scheduler job; that path is intentionally outside this module so it can
be exercised without spinning up a real Telegram Application.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.invites import InviteError, redeem_invite
from app.magic import mint_magic_link
from app.models import Assignment, User
from app.notifications import mark_chore_response, pending_assignments_for_user
from app.tg.notify import (
    chore_keyboard,
    chore_message_text,
    parse_callback,
    resolved_message_text,
)

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import Application, ContextTypes

log = logging.getLogger("choreizo.tg")


# ---------------------------------------------------------------------------
# /start — claim an invite code
# ---------------------------------------------------------------------------

WELCOME_TEMPLATE = (
    "Hi {name}! Your account is now linked.\n\n"
    "Sign in to the web UI here (link expires in 15 minutes):\n{link}"
)
ERROR_PREAMBLE = "Couldn't redeem that code: "


async def redeem_start(
    code_str: str,
    *,
    chat_id: int,
    username: str | None,
) -> tuple[bool, str]:
    if not code_str:
        return False, "Send /start <code> to claim an invite."
    async with AsyncSessionLocal() as session:
        try:
            result = await redeem_invite(
                session,
                code_str=code_str,
                telegram_chat_id=chat_id,
                telegram_username=username,
            )
        except InviteError as e:
            return False, ERROR_PREAMBLE + str(e)
        link = await mint_magic_link(session, result.user)
    return True, WELCOME_TEMPLATE.format(name=result.user.name, link=link)


async def _start_handler(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if msg is None or chat is None:
        return
    code_str = (context.args[0] if context.args else "").strip()
    ok, reply = await redeem_start(
        code_str, chat_id=chat.id, username=user.username if user else None
    )
    log.info("/start chat=%s ok=%s", chat.id, ok)
    await msg.reply_text(reply, disable_web_page_preview=True)


# ---------------------------------------------------------------------------
# Inline-button callbacks (Done/Skip/Ignore buttons on assignment messages)
# ---------------------------------------------------------------------------

async def handle_chore_callback(
    *,
    callback_data: str,
    by_chat_id: int,
) -> tuple[bool, str, str | None, int | None]:
    """Pure core for the inline-button press.

    Returns (ok, user_facing_message, new_status_or_none, assignment_id_or_none).
    The PTB handler wraps this and edits the message in place.
    """
    parsed = parse_callback(callback_data)
    if parsed is None:
        return False, "Unrecognised button.", None, None
    assignment_id, action = parsed
    async with AsyncSessionLocal() as session:
        result = await mark_chore_response(
            session,
            assignment_id=assignment_id,
            action=action,
            by_chat_id=by_chat_id,
        )
    return result.ok, result.message, result.new_status, assignment_id


async def _callback_handler(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    chat = update.effective_chat
    if chat is None:
        await query.answer("No chat context.", show_alert=False)
        return

    ok, msg, new_status, assignment_id = await handle_chore_callback(
        callback_data=query.data, by_chat_id=chat.id
    )
    await query.answer(msg, show_alert=not ok)

    if ok and assignment_id is not None and new_status is not None:
        try:
            from telegram.constants import ParseMode  # local import

            original_text = query.message.text_markdown or query.message.text or ""
            badge = {
                "completed": "✅ Done",
                "skipped":   "⏭ Skipped",
                "ignored":   "🙈 Ignored",
            }.get(new_status, new_status)
            await query.edit_message_text(
                f"{original_text}\n\n_{badge}_",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:  # pragma: no cover - editing best-effort
            log.exception("Failed to edit callback message")


# ---------------------------------------------------------------------------
# Helper: look up the User row linked to a Telegram chat_id
# ---------------------------------------------------------------------------

async def _get_linked_user(chat_id: int):
    """Return the active User linked to this chat_id, or None."""
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        from app.models import User as UserModel
        row = (
            await session.execute(
                select(UserModel)
                .where(UserModel.telegram_chat_id == chat_id)
                .where(UserModel.active.is_(True))
            )
        ).scalar_one_or_none()
        return row


# ---------------------------------------------------------------------------
# /today — resend today's pending assignments
# ---------------------------------------------------------------------------

async def _today_handler(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return

    user = await _get_linked_user(chat.id)
    if user is None:
        await msg.reply_text(
            "Your Telegram account isn't linked yet. "
            "Ask an admin for an invite code and run /start <code>."
        )
        return

    settings = get_settings()
    today_str = datetime.now(ZoneInfo(settings.house_timezone)).date().isoformat()

    async with AsyncSessionLocal() as session:
        assignments = await pending_assignments_for_user(
            session, user_id=user.id, today_str=today_str
        )

    if not assignments:
        await msg.reply_text("No pending chores for you today. 🎉")
        return

    await msg.reply_text(f"Today's chores ({len(assignments)}):")
    for a in assignments:
        await msg.reply_text(
            chore_message_text(a),
            reply_markup=chore_keyboard(a.id),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )


# ---------------------------------------------------------------------------
# /done, /skip, /ignore — text-command alternatives to inline buttons
# ---------------------------------------------------------------------------

async def _action_handler(
    action: str,
    update: "Update",
    context: "ContextTypes.DEFAULT_TYPE",
) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return

    if not context.args:
        await msg.reply_text(f"Usage: /{action} <assignment_id>")
        return

    try:
        assignment_id = int(context.args[0])
    except ValueError:
        await msg.reply_text(f"Usage: /{action} <assignment_id>  (ID must be a number)")
        return

    async with AsyncSessionLocal() as session:
        result = await mark_chore_response(
            session,
            assignment_id=assignment_id,
            action=action,
            by_chat_id=chat.id,
        )
    await msg.reply_text(result.message)


async def _done_handler(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    await _action_handler("done", update, context)


async def _skip_handler(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    await _action_handler("skip", update, context)


async def _ignore_handler(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    await _action_handler("ignore", update, context)


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

HELP_TEXT = """\
*Choreizo commands*

/today — show today's chore assignments
/done <id> — mark a chore completed
/skip <id> — skip a chore (it retries next cycle)
/ignore <id> — acknowledge without acting (closes reminders)
/web — get a sign-in link for the member web UI
/whoami — show your linked account
/help — this message

_Admins also have:_
/admin — get a sign-in link for the admin panel
"""


async def _help_handler(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(HELP_TEXT, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /whoami — show linked identity
# ---------------------------------------------------------------------------

async def _whoami_handler(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return

    user = await _get_linked_user(chat.id)
    if user is None:
        await msg.reply_text(
            "This Telegram account isn't linked to a Choreizo account yet.\n"
            "Ask an admin for an invite code and run /start <code>."
        )
        return

    role = "admin" if user.is_admin else "member"
    await msg.reply_text(
        f"You are linked as *{user.name}* ({role}).\n"
        f"Daily send time: {user.send_time}",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /web — send a magic-link for the member web UI
# ---------------------------------------------------------------------------

async def _web_handler(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return

    user = await _get_linked_user(chat.id)
    if user is None:
        await msg.reply_text(
            "Your Telegram account isn't linked yet. "
            "Ask an admin for an invite code and run /start <code>."
        )
        return

    async with AsyncSessionLocal() as session:
        # Re-fetch inside the session so the link mint can commit.
        from sqlalchemy import select as sa_select
        db_user = (
            await session.execute(
                sa_select(User).where(User.id == user.id)
            )
        ).scalar_one()
        link = await mint_magic_link(session, db_user)

    await msg.reply_text(
        f"Sign in here (link expires in 15 minutes):\n{link}",
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# /admin — send a magic-link for the admin web UI (admins only)
# ---------------------------------------------------------------------------

async def _admin_handler(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return

    user = await _get_linked_user(chat.id)
    if user is None:
        await msg.reply_text("Your account isn't linked. Use /start <code> first.")
        return

    if not user.is_admin:
        await msg.reply_text("That command is for admins only.")
        return

    async with AsyncSessionLocal() as session:
        from sqlalchemy import select as sa_select
        db_user = (
            await session.execute(
                sa_select(User).where(User.id == user.id)
            )
        ).scalar_one()
        link = await mint_magic_link(session, db_user)

    await msg.reply_text(
        f"Admin sign-in link (expires in 15 minutes):\n{link}",
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def build_application() -> "Application | None":
    settings = get_settings()
    if not settings.telegram_bot_token:
        return None
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler

    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("start",  _start_handler))
    app.add_handler(CommandHandler("today",  _today_handler))
    app.add_handler(CommandHandler("done",   _done_handler))
    app.add_handler(CommandHandler("skip",   _skip_handler))
    app.add_handler(CommandHandler("ignore", _ignore_handler))
    app.add_handler(CommandHandler("help",   _help_handler))
    app.add_handler(CommandHandler("whoami", _whoami_handler))
    app.add_handler(CommandHandler("web",    _web_handler))
    app.add_handler(CommandHandler("admin",  _admin_handler))
    app.add_handler(CallbackQueryHandler(_callback_handler))
    return app


async def start_polling(app: "Application") -> None:
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log.info("Telegram bot started (polling)")


async def stop_polling(app: "Application") -> None:
    try:
        await app.updater.stop()
    except Exception:  # pragma: no cover
        log.exception("Error stopping updater")
    await app.stop()
    await app.shutdown()
    log.info("Telegram bot stopped")


async def get_bot():
    """Return the raw Bot for the configured token, or None if unset.

    Used by the per-minute scheduler tick when the long-polling
    Application isn't easy to thread through.
    """
    settings = get_settings()
    if not settings.telegram_bot_token:
        return None
    from telegram import Bot

    return Bot(token=settings.telegram_bot_token)
