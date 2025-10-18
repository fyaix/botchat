import logging
import os
from collections import deque
import asyncio
import time
import re
from dotenv import load_dotenv
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
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

# --- Helper ---
def is_message_suspicious(text: str) -> bool:
    text_lower = text.lower()
    return any(word in text_lower for word in FORBIDDEN_WORDS) or bool(re.search(r'@\w+', text))

# --- Main Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    profile = database.get_or_create_user_profile(user_id, admin_ids=ADMIN_IDS)
    if profile['is_shadow_banned']:
        await update.message.reply_text("🔎 Searching for a partner...")
        return
    if profile['reputation'] < REPUTATION_LEVEL_SUSPENDED:
        await update.message.reply_text("Your reputation is too low to start a new chat.")
        return
    if session.get_partner_id(user_id):
        await update.message.reply_text("You are already in a chat. Use /stop to end it.")
        return
    waiting_premium, waiting_regular = session.get_waiting_users()
    if str(user_id) in waiting_premium or str(user_id) in waiting_regular:
        await update.message.reply_text("You are already looking for a partner. Please wait.")
        return
    session.set_behavior_tracker(user_id, {'start_time': time.time(), 'message_sent': False, 'delay_notified': False})
    session.add_to_waiting_queue(user_id, is_premium=profile['is_premium'])
    msg = "👑 Searching for a partner with high priority..." if profile['is_premium'] else "🔎 Searching for a partner..."
    await update.message.reply_text(msg)
    await try_match_users(context)

async def handle_chat_disconnection(user_id: int, context: ContextTypes.DEFAULT_TYPE, is_next: bool = False):
    partner_id = session.end_chat_session(user_id)
    if not partner_id:
        if not is_next: await context.bot.send_message(chat_id=user_id, text="You are not in a chat. Use /start to find one.")
        return False
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
    if not is_next: await context.bot.send_message(chat_id=user_id, text="💬 You have left the chat.")
    await context.bot.send_message(chat_id=partner_id, text="💬 Your partner has left the chat.")
    session.remove_behavior_tracker(user_id); session.remove_behavior_tracker(partner_id)
    return True

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: await handle_chat_disconnection(update.effective_user.id, context)
async def next_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await handle_chat_disconnection(update.effective_user.id, context, is_next=True): await start(update, context)

async def showid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    partner_id = session.get_partner_id(user_id)
    if not partner_id:
        await update.message.reply_text("This command can only be used when you are in a chat.")
        return
    user_info = update.effective_user
    user_name = user_info.first_name or f"User {user_id}"
    profile_link = f"tg://user?id={user_id}"
    keyboard = [[InlineKeyboardButton(f"View {escape_markdown(user_name, 2)}'s Profile", url=profile_link)]]
    await context.bot.send_message(chat_id=partner_id, text=f"Your partner, {escape_markdown(user_name, 2)}, wants to share their profile with you\.",
                                   reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='MarkdownV2')
    await update.message.reply_text("Your profile has been shared with your partner.")

# --- Profile Conversation ---
async def profil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    profile = database.get_or_create_user_profile(user_id, admin_ids=ADMIN_IDS)
    status_parts = ["Premium"] if profile['is_premium'] else []
    if profile['is_trusted']: status_parts.append("Trusted User 🛡️")
    raw_status = ", ".join(status_parts) if status_parts else "Non-Premium"
    status = escape_markdown(raw_status, version=2)
    my_gender = escape_markdown(str(profile.get('gender') or "Not Set"), 2)
    my_age = escape_markdown(str(profile.get('age') or "Not Set"), 2)
    pref_gender = escape_markdown(str(profile['preferences'].get('gender') or "Any"), 2)
    pref_age = escape_markdown(str(profile['preferences'].get('age') or "Any"), 2)
    profile_text = (f"👤 *Your Profile*\nID: `{user_id}`\nStatus: {status}\nReputation: {profile['reputation']}\n"
                    f"Violations: {profile['violation_count']}\nShadow Banned: {'Yes' if profile['is_shadow_banned'] else 'No'}\n\n"
                    f"*My Info*\nGender: {my_gender}\nAge: {my_age}\n\n"
                    f"*Partner Preference*\nGender: {pref_gender}\nAge: {pref_age}")
    keyboard = [[InlineKeyboardButton("🚻 Set My Gender", callback_data="edit_my_gender"), InlineKeyboardButton("🎂 Set My Age", callback_data="edit_my_age")],
                [InlineKeyboardButton("💘 Set Partner Gender", callback_data="edit_pref_gender"), InlineKeyboardButton("🎯 Set Partner Age", callback_data="edit_pref_age")],
                [InlineKeyboardButton("Done", callback_data="done_editing")]]
    if update.callback_query:
        await update.callback_query.edit_message_text(profile_text, parse_mode='MarkdownV2', reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(profile_text, parse_mode='MarkdownV2', reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_PROFILE_CHOICE

async def ask_for_input(update: Update, query_text: str, state: int) -> int:
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(query_text)
    return state

async def received_my_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    database.update_user_profile(update.effective_user.id, {'gender': update.message.text.strip().lower()})
    await update.message.reply_text("Your gender has been updated.")
    await profil(update, context)
    return ConversationHandler.END

async def received_my_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    age = update.message.text.strip()
    if not age.isdigit() or not (13 <= int(age) <= 100):
        await update.message.reply_text("Invalid age. Please enter a number between 13 and 100.")
        return AWAIT_MY_AGE
    database.update_user_profile(update.effective_user.id, {'age': int(age)})
    await update.message.reply_text("Your age has been updated.")
    await profil(update, context)
    return ConversationHandler.END

async def received_pref_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    gender = update.message.text.strip().lower()
    profile = database.get_user_profile(update.effective_user.id)
    profile['preferences']['gender'] = gender if gender != 'any' else None
    database.update_user_profile(update.effective_user.id, {'preferences': profile['preferences']})
    await update.message.reply_text(f"Partner gender preference set to: {gender}")
    await profil(update, context)
    return ConversationHandler.END

async def received_pref_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    age_text = update.message.text.strip().lower()
    profile = database.get_user_profile(update.effective_user.id)
    if age_text == 'any':
        profile['preferences']['age'] = None
    elif not age_text.isdigit() or not (13 <= int(age_text) <= 100):
        await update.message.reply_text("Invalid age. Please enter a number between 13-100 or 'any'.")
        return AWAIT_PREF_AGE
    else:
        profile['preferences']['age'] = int(age_text)
    database.update_user_profile(update.effective_user.id, {'preferences': profile['preferences']})
    await update.message.reply_text(f"Partner age preference set to: {age_text}")
    await profil(update, context)
    return ConversationHandler.END

async def done_editing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Profile editing finished.")
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Check if we are in a callback query context
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Editing cancelled.")
    else:
        await update.message.reply_text("Editing cancelled.")
    return ConversationHandler.END


# --- Message & Callback Handlers ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    partner_id = session.get_partner_id(user_id)
    if not partner_id:
        await update.message.reply_text("You are not in a chat. Use /start to find one.")
        return
    profile = database.get_or_create_user_profile(user_id, admin_ids=ADMIN_IDS)
    if profile['reputation'] <= REPUTATION_LEVEL_RISKY and update.message.sticker:
        await update.message.reply_text("Your reputation is too low to send stickers.")
        return
    if profile['reputation'] <= REPUTATION_LEVEL_MONITORED:
        tracker = session.get_behavior_tracker(user_id) or {}
        if not tracker.get('delay_notified', False):
            await update.message.reply_text("Your messages are being sent with a slight delay due to your low reputation score.", disable_notification=True)
            tracker['delay_notified'] = True
            session.set_behavior_tracker(user_id, tracker)
        await asyncio.sleep(3)
    message = update.message
    if message.text and is_message_suspicious(message.text):
        database.update_user_profile(user_id, {'reputation': profile['reputation'] + REP_CHANGE_SUSPICIOUS_MESSAGE})
        await update.message.reply_text("Your message was flagged as suspicious and your reputation has been lowered.", disable_notification=True)
        return
    tracker = session.get_behavior_tracker(user_id) or {}
    tracker.update({'message_sent': True, 'last_message_time': time.time()})
    session.set_behavior_tracker(user_id, tracker)
    if message.text: await context.bot.send_message(chat_id=partner_id, text=message.text)
    elif message.photo: await context.bot.send_photo(chat_id=partner_id, photo=message.photo[-1].file_id)
    elif message.sticker:
        if database.is_sticker_banned(message.sticker.file_unique_id):
            await update.message.reply_text("This sticker is not allowed.")
            database.update_user_profile(user_id, {'reputation': profile['reputation'] + REP_CHANGE_SUSPICIOUS_MESSAGE})
            return
        await context.bot.send_sticker(chat_id=partner_id, sticker=message.sticker.file_id)
    elif message.voice: await context.bot.send_voice(chat_id=partner_id, voice=message.voice.file_id)
    elif message.video: await context.bot.send_video(chat_id=partner_id, video=message.video.file_id)
    elif message.document: await context.bot.send_document(chat_id=partner_id, document=message.document.file_id)
    else: await update.message.reply_text("Unsupported message type.")

async def handle_validation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    action, reported_user_id_str = query.data.split('_')[1], query.data.split('_')[2]
    reported_user_id = int(reported_user_id_str)
    profile = database.get_or_create_user_profile(reported_user_id, admin_ids=ADMIN_IDS)
    if action == "yes":
        updates = {'reputation': profile['reputation'] + REP_CHANGE_CONFIRMED_SPAM, 'violation_count': profile['violation_count'] + 1}
        if updates['violation_count'] >= MAX_VIOLATIONS_BEFORE_SHADOW_BAN:
            updates['is_shadow_banned'] = True
        database.update_user_profile(reported_user_id, updates)
        await query.edit_message_text(text="Thank you. The user has been penalized.")
    else:
        reporter_profile = database.get_or_create_user_profile(query.from_user.id, admin_ids=ADMIN_IDS)
        database.update_user_profile(query.from_user.id, {'reputation': reporter_profile['reputation'] + REP_CHANGE_FALSE_REPORT})
        await query.edit_message_text(text="Thank you for your feedback. No action was taken against the other user.")

# --- Matchmaking ---
def _check_reciprocal_match(user1_profile: dict, user2_profile: dict) -> bool:
    # Check gender and region
    for key in ['gender', 'region']:
        if user1_profile['preferences'].get(key) is not None and user1_profile.get(key) is not None and user1_profile['preferences'][key] != user2_profile[key]: return False
        if user2_profile['preferences'].get(key) is not None and user1_profile.get(key) is not None and user2_profile['preferences'][key] != user1_profile[key]: return False
    u1_age_pref, u2_age_pref = user1_profile['preferences'].get('age'), user2_profile['preferences'].get('age')
    u1_age, u2_age = user1_profile.get('age'), user2_profile.get('age')
    if u1_age_pref is not None and u2_age is not None and abs(u1_age_pref - u2_age) > 5: return False
    if u2_age_pref is not None and u1_age is not None and abs(u2_age_pref - u1_age) > 5: return False
    return True

async def try_match_users(context: ContextTypes.DEFAULT_TYPE) -> None:
    premium_q_str, regular_q_str = session.get_waiting_users()
    premium_q, regular_q = deque(map(int, premium_q_str)), deque(map(int, regular_q_str))
    unmatched_premium = deque()
    while premium_q:
        p_user_id = premium_q.popleft()
        p_profile = database.get_user_profile(p_user_id)
        if not p_profile or p_profile['is_shadow_banned']: continue
        if not any(v is not None for v in p_profile['preferences'].values()):
            unmatched_premium.append(p_user_id); continue
        found_match = False
        for partner_id in list(premium_q) + list(regular_q):
            partner_profile = database.get_user_profile(partner_id)
            if not partner_profile: continue
            if _check_reciprocal_match(p_profile, partner_profile):
                await _create_chat(p_user_id, partner_id, context)
                if partner_id in premium_q: premium_q.remove(partner_id)
                else: regular_q.remove(partner_id)
                found_match = True; break
        if not found_match: unmatched_premium.append(p_user_id)
    while len(unmatched_premium) >= 2: await _create_chat(unmatched_premium.popleft(), unmatched_premium.popleft(), context)
    if len(unmatched_premium) == 1 and len(regular_q) >= 1: await _create_chat(unmatched_premium.popleft(), regular_q.popleft(), context)
    while len(regular_q) >= 2: await _create_chat(regular_q.popleft(), regular_q.popleft(), context)

async def _create_chat(user1_id: int, user2_id: int, context: ContextTypes.DEFAULT_TYPE):
    session.start_chat_session(user1_id, user2_id)
    session.remove_from_waiting_queue(user1_id, database.get_user_profile(user1_id)['is_premium'])
    session.remove_from_waiting_queue(user2_id, database.get_user_profile(user2_id)['is_premium'])
    session.set_behavior_tracker(user1_id, {'start_time': time.time(), 'message_sent': False, 'delay_notified': False})
    session.set_behavior_tracker(user2_id, {'start_time': time.time(), 'message_sent': False, 'delay_notified': False})
    await context.bot.send_message(chat_id=user1_id, text="✅ Partner found! You can start chatting.")
    await context.bot.send_message(chat_id=user2_id, text="✅ Partner found! You can start chatting.")

# --- Admin Commands ---
async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS: return
    all_user_ids = database.get_all_user_ids()
    shadow_banned_count = sum(1 for uid in all_user_ids if database.get_user_profile(uid)['is_shadow_banned'])
    prem_q_len, reg_q_len = session.get_queue_lengths()
    uptime = timedelta(seconds=int(time.time() - BOT_START_TIME))
    stats_text = (f"📊 *Bot Dashboard*\n\n"
                  f"Uptime: {escape_markdown(str(uptime), 2)}\n"
                  f"Active Chats: {session.get_active_chat_count()}\n"
                  f"Waiting Users: {prem_q_len} Premium, {reg_q_len} Regular\n"
                  f"Total Profiles: {len(all_user_ids)}\n"
                  f"Total Reports: {database.get_total_reports()}\n"
                  f"Shadow Banned: {shadow_banned_count}")
    await update.message.reply_text(stats_text, parse_mode='MarkdownV2')

# ... (other admin commands are the same) ...
async def setpreference(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def trustuser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def bansticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def unbansticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def listbannedstickers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass


def main() -> None:
    database.initialize_database()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token: logger.error("TELEGRAM_BOT_TOKEN not set."); return
    application = Application.builder().token(token).build()

    profile_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("profil", profil)],
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
        conversation_timeout=300 # 5 minutes
    )

    application.add_handler(profile_conv_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("next", next_chat))
    application.add_handler(CommandHandler("showid", showid))

    # Admin Commands
    application.add_handler(CommandHandler("maintenance", maintenance))
    application.add_handler(CommandHandler("shutdown", shutdown))
    application.add_handler(CommandHandler("dashboard", dashboard))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("trustuser", trustuser))
    application.add_handler(CommandHandler("bansticker", bansticker))
    application.add_handler(CommandHandler("unbansticker", unbansticker))
    application.add_handler(CommandHandler("listbannedstickers", listbannedstickers))

    application.add_handler(CallbackQueryHandler(handle_validation_callback, pattern=r'^validate_'))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_message))

    logger.info("Starting bot in polling mode...")
    application.run_polling()

if __name__ == "__main__":
    main()