import logging
import os
from collections import deque
import asyncio
import time
import re
from dotenv import load_dotenv
from datetime import datetime, timedelta

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
(EDIT_PROFILE_CHOICE, AWAIT_MY_GENDER, AWAIT_MY_AGE,
 AWAIT_PREF_GENDER, AWAIT_PREF_AGE) = range(5)

# --- Reply Keyboard ---
MAIN_KEYBOARD = [
    ["Search (Random) 🎲", "Search by Gender 🚻"],
    ["Profile 👤"]
]
MAIN_REPLY_MARKUP = ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True)


# --- Helper ---
def is_message_suspicious(text: str) -> bool:
    text_lower = text.lower()
    return any(word in text_lower for word in FORBIDDEN_WORDS) or bool(re.search(r'@\w+', text))

async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str):
    """Sends a message with the main reply keyboard."""
    await update.message.reply_text(message_text, reply_markup=MAIN_REPLY_MARKUP)

# --- Main Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    database.get_or_create_user_profile(user_id, admin_ids=ADMIN_IDS)
    await send_main_menu(update, context, "Welcome to the bot! Use the buttons below to navigate.")

async def start_search_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, gender_filter: str | None = None):
    user_id = update.effective_user.id
    profile = database.get_or_create_user_profile(user_id, admin_ids=ADMIN_IDS)

    message = update.message
    if profile['is_shadow_banned']:
        await message.reply_text("🔎 Searching for a partner...")
        return
    if profile['reputation'] < REPUTATION_LEVEL_SUSPENDED:
        await message.reply_text("Your reputation is too low to start a new chat.")
        return
    if session.get_partner_id(user_id):
        await message.reply_text("You are already in a chat. Use /stop to end it.")
        return
    waiting_premium, waiting_regular = session.get_waiting_users()
    if str(user_id) in waiting_premium or str(user_id) in waiting_regular:
        await message.reply_text("You are already looking for a partner. Please wait.")
        return

    session.set_behavior_tracker(user_id, {'start_time': time.time(), 'message_sent': False, 'delay_notified': False})

    if gender_filter:
        session.set_active_filter(user_id, gender_filter)
        search_message = f"🔎 Searching for a {gender_filter} partner..."
    else:
        session.clear_active_filter(user_id)
        search_message = "🔎 Searching for a random partner..."

    session.add_to_waiting_queue(user_id, is_premium=profile['is_premium'])
    await message.reply_text(search_message, reply_markup=ReplyKeyboardMarkup([["/stop", "/next"]], resize_keyboard=True))
    await try_match_users(context)

async def handle_chat_disconnection(user_id: int, context: ContextTypes.DEFAULT_TYPE, is_next: bool = False):
    partner_id = session.end_chat_session(user_id)
    if not partner_id:
        if not is_next: await context.bot.send_message(chat_id=user_id, text="You are not in a chat. Use /start to find one.")
        return False, None

    if not is_next:
        session.clear_active_filter(user_id)
        session.clear_active_filter(partner_id)

    tracker = session.get_behavior_tracker(user_id)
    if tracker:
        chat_duration = time.time() - tracker.get('start_time', 0)
        message_sent = tracker.get('message_sent', False)
        if message_sent and chat_duration < SUSPICIOUS_SKIP_TIME_SECONDS:
            profile = database.get_user_profile(user_id)
            database.update_user_profile(user_id, {'reputation': profile['reputation'] + REP_CHANGE_REPORTED_ONCE})
            database.increment_total_reports()
            keyboard = [[InlineKeyboardButton("✅ Yes", callback_data=f"validate_yes_{user_id}"), InlineKeyboardButton("❌ No", callback_data=f"validate_no_{user_id}")]]
            await context.bot.send_message(chat_id=partner_id, text="⚠️ Your partner left right after sending a message. Was it a vulgar or promotional message?", reply_markup=InlineKeyboardMarkup(keyboard))
        elif chat_duration > 60:
            database.update_user_profile(user_id, {'reputation': database.get_user_profile(user_id)['reputation'] + REP_CHANGE_NORMAL_CHAT})
            database.update_user_profile(partner_id, {'reputation': database.get_user_profile(partner_id)['reputation'] + REP_CHANGE_NORMAL_CHAT})

    if not is_next:
        await context.bot.send_message(chat_id=user_id, text="💬 You have left the chat.", reply_markup=MAIN_REPLY_MARKUP)
    await context.bot.send_message(chat_id=partner_id, text="💬 Your partner has left the chat.", reply_markup=MAIN_REPLY_MARKUP)
    session.remove_behavior_tracker(user_id); session.remove_behavior_tracker(partner_id)
    return True, partner_id

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_chat_disconnection(update.effective_user.id, context)

async def next_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    active_filter = session.get_active_filter(user_id)

    success, _ = await handle_chat_disconnection(user_id, context, is_next=True)
    if success:
        if active_filter and 'gender' in active_filter:
            await start_search_flow(update, context, gender_filter=active_filter['gender'])
        else:
            await start_search_flow(update, context)

async def showid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def profil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: pass
async def ask_for_input(update: Update, query_text: str, state: int) -> int: pass
async def received_my_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: pass
async def received_my_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: pass
async def received_pref_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: pass
async def received_pref_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: pass
async def done_editing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: pass
async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int: pass
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def handle_validation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def search_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
def _check_reciprocal_match(user1_profile: dict, user2_profile: dict) -> bool: pass
async def try_match_users(context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def _create_chat(user1_id: int, user2_id: int, context: ContextTypes.DEFAULT_TYPE): pass
async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def trustuser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def bansticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def unbansticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def listbannedstickers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass

async def handle_keyboard_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    if text == "Search (Random) 🎲":
        await start_search_flow(update, context)
    elif text == "Search by Gender 🚻":
        user_id = update.effective_user.id
        profile = database.get_or_create_user_profile(user_id, admin_ids=ADMIN_IDS)
        if not profile['is_premium']:
            keyboard = [[InlineKeyboardButton("Buy Premium", callback_data="pay_premium")]]
            await update.message.reply_text(
                "Search by gender is a premium feature. Please upgrade to use it.\n\n"
                "Premium Prices:\n- 1 Month: $5\n- 3 Months: $12\n- 1 Year: $40",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            keyboard = [[InlineKeyboardButton("Male", callback_data="search_gender_male"), InlineKeyboardButton("Female", callback_data="search_gender_female")]]
            await update.message.reply_text("Please choose the gender you want to search for:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif text == "Profile 👤":
        await profil(update, context)


def main() -> None:
    database.initialize_database()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token: logger.error("TELEGRAM_BOT_TOKEN not set."); return
    application = Application.builder().token(token).build()

    profile_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Profile 👤$"), profil)],
        states={
            EDIT_PROFILE_CHOICE: [
                CallbackQueryHandler(lambda u,c: ask_for_input(u, "Please tell me your gender (e.g., male, female, other).", AWAIT_MY_GENDER), pattern="^edit_my_gender$"),
                CallbackQueryHandler(lambda u,c: ask_for_input(u, "Please tell me your age.", AWAIT_MY_AGE), pattern="^edit_my_age$"),
                CallbackQueryHandler(lambda u,c: ask_for_input(u, "What gender are you looking for? (e.g., male, female, any)", AWAIT_PREF_GENDER), pattern="^edit_pref_gender$"),
                CallbackQueryHandler(lambda u,c: ask_for_input(u, "What age are you looking for? (e.g., 25, any)", AWAIT_PREF_AGE), pattern="^edit_pref_age$"),
                CallbackQueryHandler(done_editing, pattern="^done_editing$")
            ],
            AWAIT_MY_GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_my_gender)],
            AWAIT_MY_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_my_age)],
            AWAIT_PREF_GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_pref_gender)],
            AWAIT_PREF_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_pref_age)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        conversation_timeout=300
    )

    application.add_handler(profile_conv_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("next", next_chat))
    application.add_handler(CommandHandler("showid", showid))
    application.add_handler(CommandHandler("pay", pay))

    # Admin Commands
    # ... (admin handlers) ...

    application.add_handler(CallbackQueryHandler(search_callback_handler, pattern="^search_gender_"))
    application.add_handler(CallbackQueryHandler(lambda u,c: pay(u.callback_query.message, c), pattern="^pay_premium$"))
    application.add_handler(CallbackQueryHandler(handle_validation_callback, pattern=r'^validate_'))

    # Handler for main keyboard buttons
    application.add_handler(MessageHandler(filters.Regex("^(Search \(Random\) 🎲|Search by Gender 🚻|Profile 👤)$"), handle_keyboard_buttons))

    # General message handler for when in chat
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_message))

    logger.info("Starting bot in polling mode...")
    application.run_polling()

if __name__ == "__main__":
    main()