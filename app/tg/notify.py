"""Send daily chore DMs and react to inline-button presses.

PTB integration is intentionally thin — most of the work is in
app/notifications.py so it's testable without a real Bot.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models import Assignment
from app.notifications import (
    SendBatch,
    log_daily_send,
    users_needing_send,
)

if TYPE_CHECKING:
    from telegram import Bot, InlineKeyboardMarkup

log = logging.getLogger("choreizo.tg.notify")


CALLBACK_PREFIX = "assign"


def callback_for(assignment_id: int, action: str) -> str:
    """Build a callback_data string for an inline button press.

    Format: "assign:<id>:<action>" — short and within Telegram's 64-byte
    callback_data limit even for big assignment IDs.
    """
    return f"{CALLBACK_PREFIX}:{assignment_id}:{action}"


def parse_callback(data: str) -> tuple[int, str] | None:
    """Inverse of callback_for. Returns (assignment_id, action) or None."""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        return None
    try:
        return int(parts[1]), parts[2]
    except ValueError:
        return None


def chore_message_text(assignment: Assignment) -> str:
    chore = assignment.chore
    lines = [f"🧹 *{chore.name}*"]
    if chore.description:
        lines.append(chore.description)
    bits = [f"every {chore.frequency_days}d"]
    if chore.estimated_minutes:
        bits.append(f"~{chore.estimated_minutes} min")
    if chore.priority == 1:
        bits.append("high priority")
    lines.append("_" + " · ".join(bits) + "_")
    return "\n".join(lines)


def chore_keyboard(assignment_id: int) -> "InlineKeyboardMarkup":
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Done",   callback_data=callback_for(assignment_id, "done")),
        InlineKeyboardButton("⏭ Skip",   callback_data=callback_for(assignment_id, "skip")),
        InlineKeyboardButton("🙈 Ignore", callback_data=callback_for(assignment_id, "ignore")),
    ]])


STATUS_BADGE = {
    "completed": "✅ Done",
    "skipped":   "⏭ Skipped",
    "ignored":   "🙈 Ignored",
}


def resolved_message_text(assignment: Assignment, new_status: str) -> str:
    base = chore_message_text(assignment)
    badge = STATUS_BADGE.get(new_status, new_status)
    return f"{base}\n\n_{badge}_"


# -- Sending ------------------------------------------------------------------


async def _send_one_batch(bot: "Bot", batch: SendBatch) -> None:
    """Best-effort: even if one message fails we keep going for the rest."""
    chat_id = batch.user.telegram_chat_id
    if chat_id is None:
        return
    intro = f"Today's chores ({len(batch.assignments)}):"
    await bot.send_message(chat_id=chat_id, text=intro)
    for a in batch.assignments:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=chore_message_text(a),
                reply_markup=chore_keyboard(a.id),
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except Exception:  # pragma: no cover - network/PTB errors
            log.exception("Failed to send assignment %s", a.id)


async def tick(bot: "Bot", *, now_local: datetime | None = None) -> int:
    """Run one notification pass. Returns the number of users sent to."""
    settings = get_settings()
    if now_local is None:
        now_local = datetime.now(ZoneInfo(settings.house_timezone))

    sent = 0
    async with AsyncSessionLocal() as session:
        batches = await users_needing_send(session, now_local=now_local)
        for batch in batches:
            await _send_one_batch(bot, batch)
            await log_daily_send(session, assignments=batch.assignments)
            sent += 1
    if sent:
        log.info("tick: sent daily DMs to %d user(s)", sent)
    return sent
