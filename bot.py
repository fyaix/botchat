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

# --- Helper ---
def is_message_suspicious(message: Update.message) -> bool:
    text_to_check = message.text or message.caption
    if not text_to_check: return False
    text_lower = text_to_check.lower()
    suspicious_patterns = re.compile(r"(vc|vid ?call|open ?bo|nude|vcs|join group|follow @|@[\w\d_]+)", re.IGNORECASE)
    if suspicious_patterns.search(text_lower):
        if "vc kantor" in text_lower or "join komunitas" in text_lower: return False
        return True
    return False

def is_user_premium_active(profile: dict) -> bool:
    if not profile or not profile.get('is_premium'): return False
    premium_until_str = profile.get('premium_until')
    if not premium_until_str: return False
    premium_until = datetime.fromisoformat(premium_until_str)
    return premium_until > datetime.utcnow()

# --- Main Commands & Keyboard Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    database.get_or_create_user_profile(user_id, admin_ids=ADMIN_IDS)
    welcome_text = ("👋 *Welcome to the Anonymous Chat Bot\\!* \n\n"
                    "You can start searching for a partner using the buttons below\\.\n\n"
                    "*Available Commands:*\n"
                    "`/start` \\- Shows this welcome message\n"
                    "`/search` \\- Start a random search\n"
                    "`/profil` \\- View and edit your profile\n"
                    "`/stop` \\- Stop your current chat\n"
                    "`/next` \\- Find a new chat partner\n"
                    "`/showid` \\- Share your profile with your partner")
    await update.message.reply_text(welcome_text, reply_markup=MAIN_REPLY_MARKUP, parse_mode='MarkdownV2')

async def start_search_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, gender_filter: str | None = None):
    user_id = update.effective_user.id
    profile = database.get_or_create_user_profile(user_id, admin_ids=ADMIN_IDS)
    message = update.message
    if profile['is_shadow_banned']:
        await message.reply_text("⏳ Searching for a partner...", reply_markup=IN_CHAT_REPLY_MARKUP); return
    if profile['reputation'] < REPUTATION_LEVEL_SUSPENDED:
        await message.reply_text("Your reputation is too low to start a new chat.", reply_markup=MAIN_REPLY_MARKUP); return
    if session.get_partner_id(user_id):
        await message.reply_text("You are already in a chat. Use /stop to end it.", reply_markup=IN_CHAT_REPLY_MARKUP); return
    waiting_premium, waiting_regular = session.get_waiting_users()
    if str(user_id) in waiting_premium or str(user_id) in waiting_regular:
        await message.reply_text("You are already looking for a partner. Please wait."); return
    session.set_behavior_tracker(user_id, {'start_time': time.time(), 'message_sent': False, 'delay_notified': False})
    if gender_filter:
        session.set_active_filter(user_id, gender_filter)
        search_message = f"🔎 Searching for a {gender_filter} partner..."
    else:
        session.clear_active_filter(user_id)
        search_message = "🔎 Searching for a random partner..."
    session.add_to_waiting_queue(user_id, is_premium=is_user_premium_active(profile))
    await message.reply_text(search_message, reply_markup=IN_CHAT_REPLY_MARKUP)
    await try_match_users(context)

async def handle_chat_disconnection(user_id: int, context: ContextTypes.DEFAULT_TYPE, is_next: bool = False): pass
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def next_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
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
def _check_reciprocal_match(user1_profile: dict, user2_profile: dict) -> bool: pass
async def try_match_users(context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def _create_chat(user1_id: int, user2_id: int, context: ContextTypes.DEFAULT_TYPE): pass
async def maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def trustuser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def bansticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def unbansticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def listbannedstickers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def generatecode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def redeem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def report_partner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def manual_report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def handle_keyboard_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)
    error_message = (f"An exception was raised while handling an update\n"
                     f"<pre>{escape_markdown(str(context.error), 2)}</pre>\n\n"
                     f"Traceback:\n<pre>{escape_markdown(tb_string, 2)}</pre>")
    for admin_id in ADMIN_IDS:
        await context.bot.send_message(chat_id=admin_id, text=error_message, parse_mode='HTML')

# --- Admin Commands ---
async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS: return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    user_id_to_ban = int(context.args[0])
    database.update_user_profile(user_id_to_ban, {'is_shadow_banned': True, 'reputation': 0})
    database.log_event(update.effective_user.id, "manual_ban", f"Banned user {user_id_to_ban}")
    await update.message.reply_text(f"User {user_id_to_ban} has been manually banned and their reputation set to 0.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS: return
    all_user_ids = database.get_all_user_ids()
    active_24h = database.get_active_users_24h()
    premium_users = [uid for uid in all_user_ids if is_user_premium_active(database.get_user_profile(uid))]
    shadow_banned_count = sum(1 for uid in all_user_ids if database.get_user_profile(uid)['is_shadow_banned'])
    prem_q_len, reg_q_len = session.get_queue_lengths()
    uptime = timedelta(seconds=int(time.time() - BOT_START_TIME))
    stats_text = (f"📊 *Bot Statistics*\n\n"
                  f"🕒 Uptime: {escape_markdown(str(uptime), 2)}\n"
                  f"👥 Total Users: {len(all_user_ids)}\n"
                  f"🟢 Active Users (24h): {active_24h}\n"
                  f"⭐ Premium Users: {len(premium_users)}\n"
                  f"🔗 Active Chats: {session.get_active_chat_count()}\n"
                  f"⏳ Waiting Users: {prem_q_len} Premium, {reg_q_len} Regular\n"
                  f"📈 Total Successful Chats: {database.get_stat('successful_chats')}\n"
                  f"🚨 Total Reports: {database.get_stat('total_reports')}\n"
                  f"👻 Shadow Banned: {shadow_banned_count}")
    await update.message.reply_text(stats_text, parse_mode='MarkdownV2')

def main() -> None:
    database.initialize_database()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token: logger.error("TELEGRAM_BOT_TOKEN not set."); return
    application = Application.builder().token(token).build()

    # ... (handlers) ...
    application.add_handler(CommandHandler("ban", ban))
    application.add_handler(CommandHandler("stats", stats))
    application.add_error_handler(error_handler)

    logger.info("Starting bot in polling mode...")
    application.run_polling()

if __name__ == "__main__":
    main()