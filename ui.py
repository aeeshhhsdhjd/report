from __future__ import annotations

"""Inline keyboard builders for the reporting bot."""

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import config

# Normalized reasons mapping
REPORT_REASONS = {
    "spam": ("Spam", 0),
    "violence": ("Violence", 1),
    "pornography": ("Pornography", 2),
    "child": ("Child Abuse", 3),
    "copyright": ("Copyright", 4),
    "fake": ("Fake", 6),
    "other": ("Other", 9),
}

def owner_panel(user_id: int | None = None) -> InlineKeyboardMarkup:
    """Owner dashboard with management shortcuts."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Start New Report", callback_data="sudo:start")],
            [InlineKeyboardButton("📂 Manage Sessions", callback_data="owner:manage")],
            [
                InlineKeyboardButton("⚙️ Session Group", callback_data="owner:set_session_group"),
                InlineKeyboardButton("📜 Logs Group", callback_data="owner:set_logs_group")
            ],
            [InlineKeyboardButton("🔄 Restart Bot", callback_data="owner:restart")]
        ]
    )

def sudo_panel(user_id: int) -> InlineKeyboardMarkup:
    """Panel for sudo users to begin a report."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Start Report", callback_data="sudo:start")]
    ])

def report_type_keyboard() -> InlineKeyboardMarkup:
    """Choose visibility of the target."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🌐 Public", callback_data="report:type:public"),
                InlineKeyboardButton("🔒 Private", callback_data="report:type:private")
            ],
        ]
    )

def reason_keyboard() -> InlineKeyboardMarkup:
    """Generates buttons based on the REPORT_REASONS dictionary."""
    buttons = []
    # Create a 2-column layout for reasons to save screen space
    reasons = list(REPORT_REASONS.items())
    for i in range(0, len(reasons), 2):
        row = [
            InlineKeyboardButton(reasons[i][1][0], callback_data=f"report:reason:{reasons[i][0]}")
        ]
        if i + 1 < len(reasons):
            row.append(
                InlineKeyboardButton(reasons[i+1][1][0], callback_data=f"report:reason:{reasons[i+1][0]}")
            )
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)

def report_count_keyboard() -> InlineKeyboardMarkup:
    """
    Optional: If you want users to click instead of typing.
    Matches MIN/MAX config bounds.
    """
    mid = (config.MIN_REPORTS + config.MAX_REPORTS) // 2
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"Low ({config.MIN_REPORTS})", callback_data=f"report:count:{config.MIN_REPORTS}")],
            [InlineKeyboardButton(f"Medium ({mid})", callback_data=f"report:count:{mid}")],
            [InlineKeyboardButton(f"Max ({config.MAX_REPORTS})", callback_data=f"report:count:{config.MAX_REPORTS}")],
        ]
    )

def queued_message(position: int) -> str:
    """User-facing queue notification text."""
    if position <= 1:
        return "🚀 **Starting your report now...**"
    
    return (
        "⏳ **Another report is currently in progress.**\n\n"
        f"Position in queue: `{position}`\n"
        "Please wait, the bot will notify you automatically when it's your turn."
    )
