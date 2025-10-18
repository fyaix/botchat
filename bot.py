import logging
import os
from collections import deque
import asyncio
import time
import re
from dotenv import load_dotenv
from datetime import datetime, timedelta
import uuid
import traceback

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler, ConversationHandler
)
from telegram.helpers import escape_markdown

import database
import session

load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

ADMIN_IDS = set(int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(',') if admin_id)
BOT_START_TIME = time.time()

# Constants
(REPUTATION_LEVEL_MONITORED, REPUTATION_LEVEL_RISKY, REPUTATION_LEVEL_SUSPENDED) = (79, 59, 40)
MAX_VIOLATIONS_BEFORE_SHADOW_BAN = 3
SUSPICIOUS_SKIP_TIME_SECONDS = 5
(REP_CHANGE_NORMAL_CHAT, REP_CHANGE_REPORTED_ONCE, REP_CHANGE_SUSPICIOUS_MESSAGE,
 REP_CHANGE_CONFIRMED_SPAM, REP_CHANGE_FALSE_REPORT) = (5, -3, -2, -5, -2)
FORBIDDEN_WORDS = {"spam", "promo", "sale", "vulgarword"}

# Conversation states
(AWAIT_MY_GENDER, AWAIT_MY_AGE, AWAIT_PREF_GENDER, AWAIT_PREF_AGE) = range(4)

# --- Reply Keyboards ---
MAIN_KEYBOARD = [["Search (Random) 🎲", "Search by Gender 🚻"], ["Profile 👤"]]
GENDER_CHOICE_KEYBOARD = [["👨 Male", "👩 Female"], ["↩️ Cancel"]]
IN_CHAT_KEYBOARD = [["/stop", "/next", "🚫 Report Partner"]]
MAIN_REPLY_MARKUP = ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True)
GENDER_CHOICE_REPLY_MARKUP = ReplyKeyboardMarkup(GENDER_CHOICE_KEYBOARD, resize_keyboard=True)
IN_CHAT_REPLY_MARKUP = ReplyKeyboardMarkup(IN_CHAT_KEYBOARD, resize_keyboard=True)

# --- (All helper and command functions are the same as before) ---
# ...

def main() -> None:
    database.initialize_database()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set.")
        return

    application = Application.builder().token(token).build()

    # --- Handler Registration Order is CRITICAL ---

    # 1. Add ConversationHandlers first, as they are complex state machines.
    profile_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("profil", profil), MessageHandler(filters.Regex("^Profile 👤$"), profil)],
        states={
            AWAIT_MY_GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_my_gender)],
            AWAIT_MY_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_my_age)],
            AWAIT_PREF_GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_pref_gender)],
            AWAIT_PREF_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_pref_age)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        map_to_parent={ ConversationHandler.END: -1 } # Example for nesting, not used here but good practice
    )
    application.add_handler(profile_conv_handler)

    # 2. Add specific CommandHandlers.
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("search", lambda u,c: start_search_flow(u,c)))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("next", next_chat))
    application.add_handler(CommandHandler("showid", showid))
    application.add_handler(CommandHandler("pay", pay))
    application.add_handler(CommandHandler("premium", premium))

    # 3. Add Admin CommandHandlers.
    application.add_handler(CommandHandler("maintenance", maintenance))
    application.add_handler(CommandHandler("shutdown", shutdown))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("ban", ban))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("trustuser", trustuser))
    application.add_handler(CommandHandler("bansticker", bansticker))
    application.add_handler(CommandHandler("unbansticker", unbansticker))
    application.add_handler(CommandHandler("listbannedstickers", listbannedstickers))
    application.add_handler(CommandHandler("generatecode", generatecode))
    application.add_handler(CommandHandler("redeem", redeem))

    # 4. Add CallbackQueryHandlers for inline buttons.
    application.add_handler(CallbackQueryHandler(manual_report_callback, pattern=r'^manual_report_'))
    application.add_handler(CallbackQueryHandler(handle_validation_callback, pattern=r'^validate_'))

    # 5. Add MessageHandler for Reply Keyboard buttons. This needs to be before the general message handler.
    application.add_handler(MessageHandler(filters.Regex("^(Search \(Random\) 🎲|Search by Gender 🚻|Profile 👤|👨 Male|👩 Female|↩️ Cancel|🚫 Report Partner)$"), handle_keyboard_buttons))

    # 6. Add the general message handler for in-chat messages LAST.
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_message))

    # 7. Add the error handler.
    application.add_error_handler(error_handler)

    logger.info("Starting bot in polling mode...")
    application.run_polling()

if __name__ == "__main__":
    main()