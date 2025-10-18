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
    if not profile:
        await update.message.reply_text("Sorry, there was a problem accessing your profile. Please try again later.")
        return

    message = update.message
    if profile['is_shadow_banned']:
        await message.reply_text("🔎 Searching for a partner...", reply_markup=IN_CHAT_REPLY_MARKUP); return
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

async def handle_chat_disconnection(user_id: int, context: ContextTypes.DEFAULT_TYPE, is_next: bool = False):
    partner_id = session.end_chat_session(user_id)
    if not partner_id:
        if not is_next: await context.bot.send_message(chat_id=user_id, text="You are not in a chat. Use /start to find one.", reply_markup=MAIN_REPLY_MARKUP)
        return False
    if not is_next:
        session.clear_active_filter(user_id); session.clear_active_filter(partner_id)
    tracker = session.get_behavior_tracker(user_id)
    if tracker:
        chat_duration = time.time() - tracker.get('start_time', 0)
        message_sent = tracker.get('message_sent', False)
        if message_sent and chat_duration < SUSPICIOUS_SKIP_TIME_SECONDS:
            profile = database.get_user_profile(user_id)
            if profile:
                database.update_user_profile(user_id, {'reputation': profile['reputation'] + REP_CHANGE_REPORTED_ONCE})
                database.increment_stat('total_reports')
                keyboard = [[InlineKeyboardButton("Ya, Laporkan 🚫", callback_data=f"validate_yes_{user_id}"), InlineKeyboardButton("Tidak, Aman ✅", callback_data=f"validate_no_{user_id}")]]
                await context.bot.send_message(chat_id=partner_id, text="⚠️ Your partner left right after sending a message. Was it a vulgar or promotional message?", reply_markup=InlineKeyboardMarkup(keyboard))
        elif chat_duration > 60:
            profile1 = database.get_user_profile(user_id)
            profile2 = database.get_user_profile(partner_id)
            if profile1: database.update_user_profile(user_id, {'reputation': profile1['reputation'] + REP_CHANGE_NORMAL_CHAT})
            if profile2: database.update_user_profile(partner_id, {'reputation': profile2['reputation'] + REP_CHANGE_NORMAL_CHAT})
    if not is_next: await context.bot.send_message(chat_id=user_id, text="💬 You have left the chat.", reply_markup=MAIN_REPLY_MARKUP)
    await context.bot.send_message(chat_id=partner_id, text="💬 Your partner has left the chat.", reply_markup=MAIN_REPLY_MARKUP)
    session.remove_behavior_tracker(user_id); session.remove_behavior_tracker(partner_id)
    return True

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: await handle_chat_disconnection(update.effective_user.id, context)
async def next_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    active_filter = session.get_active_filter(user_id)
    success, _ = await handle_chat_disconnection(user_id, context, is_next=True)
    if success:
        if active_filter and 'gender' in active_filter:
            await start_search_flow(update, context, gender_filter=active_filter['gender'])
        else:
            await start_search_flow(update, context)

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
    await context.bot.send_message(chat_id=partner_id, text=f"Your partner, {escape_markdown(user_name, 2)}, wants to share their profile with you\\.",
                                   reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='MarkdownV2')
    await update.message.reply_text("Your profile has been shared with your partner.")

async def profil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    profile = database.get_or_create_user_profile(user_id, admin_ids=ADMIN_IDS)
    if not profile:
        await update.message.reply_text("Sorry, there was a problem accessing your profile. Please try again later.")
        return ConversationHandler.END
    status_parts = ["Premium"] if is_user_premium_active(profile) else []
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
                [InlineKeyboardButton("Back to Main Menu", callback_data="done_editing")]]
    if update.callback_query:
        await update.callback_query.edit_message_text(profile_text, parse_mode='MarkdownV2', reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(profile_text, parse_mode='MarkdownV2')
    return AWAIT_MY_GENDER

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
    if profile:
        profile['preferences']['gender'] = gender if gender != 'any' else None
        database.update_user_profile(update.effective_user.id, {'preferences': profile['preferences']})
        await update.message.reply_text(f"Partner gender preference set to: {gender}")
    await profil(update, context)
    return ConversationHandler.END

async def received_pref_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    age_text = update.message.text.strip().lower()
    profile = database.get_user_profile(update.effective_user.id)
    if profile:
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
    await update.callback_query.message.delete()
    await start(update.callback_query, context)
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await start(update, context)
    return ConversationHandler.END

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    partner_id = session.get_partner_id(user_id)
    if not partner_id:
        await update.message.reply_text("You are not in a chat. Use the buttons below or /start to find one.", reply_markup=MAIN_REPLY_MARKUP)
        return
    profile = database.get_or_create_user_profile(user_id, admin_ids=ADMIN_IDS)
    if not profile: return # Defensive check
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
    if is_message_suspicious(message):
        database.update_user_profile(user_id, {'reputation': profile['reputation'] + REP_CHANGE_SUSPICIOUS_MESSAGE})
        database.log_event(user_id, "suspicious_message_detected", message.text or "Media with caption")
        keyboard = [[InlineKeyboardButton("Ya, Laporkan 🚫", callback_data=f"report_yes_{user_id}"), InlineKeyboardButton("Tidak, Aman ✅", callback_data=f"report_no_{user_id}")]]
        await context.bot.send_message(chat_id=partner_id,text="⚠️ **Pesan Mencurigakan Terdeteksi**\nApakah pesan terakhir dari partner Anda berisi promosi, spam, atau konten vulgar?", reply_markup=InlineKeyboardMarkup(keyboard))
        await context.bot.send_message(chat_id=partner_id, text=f"_{escape_markdown('Pesan di atas ditandai sebagai berpotensi spam. Abaikan jika aman.', 2)}_", parse_mode='MarkdownV2')

    tracker = session.get_behavior_tracker(user_id) or {}
    tracker.update({'message_sent': True, 'last_message_time': time.time()})
    session.set_behavior_tracker(user_id, tracker)
    if message.text: await context.bot.send_message(chat_id=partner_id, text=message.text)
    elif message.photo: await context.bot.send_photo(chat_id=partner_id, photo=message.photo[-1].file_id, caption=message.caption)
    elif message.video: await context.bot.send_video(chat_id=partner_id, video=message.video.file_id, caption=message.caption)
    elif message.sticker:
        if database.is_sticker_banned(message.sticker.file_unique_id):
            await update.message.reply_text("This sticker is not allowed.")
            database.update_user_profile(user_id, {'reputation': profile['reputation'] + REP_CHANGE_SUSPICIOUS_MESSAGE})
            return
        await context.bot.send_sticker(chat_id=partner_id, sticker=message.sticker.file_id)
    elif message.voice: await context.bot.send_voice(chat_id=partner_id, voice=message.voice.file_id)
    elif message.document: await context.bot.send_document(chat_id=partner_id, document=message.document.file_id)
    else: await update.message.reply_text("Unsupported message type.")

async def handle_validation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    action, reported_user_id_str = query.data.split('_')[1], query.data.split('_')[2]
    reported_user_id = int(reported_user_id_str)
    profile = database.get_or_create_user_profile(reported_user_id, admin_ids=ADMIN_IDS)
    if not profile: return
    if action == "yes":
        updates = {'reputation': profile['reputation'] + REP_CHANGE_CONFIRMED_SPAM, 'violation_count': profile['violation_count'] + 1}
        if updates['violation_count'] >= MAX_VIOLATIONS_BEFORE_SHADOW_BAN:
            updates['is_shadow_banned'] = True
        database.update_user_profile(reported_user_id, updates)
        await query.edit_message_text(text="Thank you. The user has been penalized.")
    else:
        reporter_profile = database.get_or_create_user_profile(query.from_user.id, admin_ids=ADMIN_IDS)
        if reporter_profile:
            database.update_user_profile(query.from_user.id, {'reputation': reporter_profile['reputation'] + REP_CHANGE_FALSE_REPORT})
        await query.edit_message_text(text="Thank you for your feedback. No action was taken against the other user.")

def _check_reciprocal_match(user1_profile: dict, user2_profile: dict) -> bool:
    if not user1_profile or not user2_profile: return False
    u1_prefs = user1_profile.get('preferences', {})
    u2_prefs = user2_profile.get('preferences', {})
    for key in ['gender', 'region']:
        if u1_prefs.get(key) is not None and user2_profile.get(key) is not None and u1_prefs[key] != user2_profile[key]: return False
        if u2_prefs.get(key) is not None and user1_profile.get(key) is not None and u2_prefs[key] != user1_profile[key]: return False
    u1_age_pref, u2_age_pref = u1_prefs.get('age'), u2_prefs.get('age')
    u1_age, u2_age = user1_profile.get('age'), user2_profile.get('age')
    if u1_age_pref is not None and u2_age is not None and abs(u1_age_pref - u2_age) > 5: return False
    if u2_age_pref is not None and u1_age is not None and abs(u2_age_pref - u1_age) > 5: return False
    return True

async def try_match_users(context: ContextTypes.DEFAULT_TYPE) -> None:
    premium_q_str, regular_q_str = session.get_waiting_users()
    p_q, r_q = deque(map(int, premium_q_str)), deque(map(int, regular_q_str))

    unmatched_p_with_prefs, p_without_prefs = deque(), deque()
    while p_q:
        user_id = p_q.popleft()
        profile = database.get_user_profile(user_id)
        if not profile or profile.get('is_shadow_banned'): continue
        if any(v is not None for v in profile.get('preferences', {}).values()):
            unmatched_p_with_prefs.append(user_id)
        else:
            p_without_prefs.append(user_id)

    still_unmatched_p = deque()
    while unmatched_p_with_prefs:
        user_id = unmatched_p_with_prefs.popleft()
        user_profile = database.get_user_profile(user_id)
        if not user_profile: continue
        found_match = False
        potential_partners = list(unmatched_p_with_prefs) + list(p_without_prefs) + list(r_q)
        for partner_id in potential_partners:
            partner_profile = database.get_user_profile(partner_id)
            if not partner_profile: continue
            if _check_reciprocal_match(user_profile, partner_profile):
                await _create_chat(user_id, partner_id, context)
                if partner_id in unmatched_p_with_prefs: unmatched_p_with_prefs.remove(partner_id)
                elif partner_id in p_without_prefs: p_without_prefs.remove(partner_id)
                elif partner_id in r_q: r_q.remove(partner_id)
                found_match = True; break
        if not found_match: still_unmatched_p.append(user_id)

    p_q = still_unmatched_p + p_without_prefs
    while len(p_q) >= 2: await _create_chat(p_q.popleft(), p_q.popleft(), context)
    if len(p_q) == 1 and r_q: await _create_chat(p_q.popleft(), r_q.popleft(), context)
    while len(r_q) >= 2: await _create_chat(r_q.popleft(), r_q.popleft(), context)

async def _create_chat(user1_id: int, user2_id: int, context: ContextTypes.DEFAULT_TYPE):
    session.start_chat_session(user1_id, user2_id)
    profile1 = database.get_user_profile(user1_id)
    profile2 = database.get_user_profile(user2_id)
    if not profile1 or not profile2: return # Should not happen
    session.remove_from_waiting_queue(user1_id, is_user_premium_active(profile1))
    session.remove_from_waiting_queue(user2_id, is_user_premium_active(profile2))
    session.set_behavior_tracker(user1_id, {'start_time': time.time(), 'message_sent': False, 'delay_notified': False})
    session.set_behavior_tracker(user2_id, {'start_time': time.time(), 'message_sent': False, 'delay_notified': False})
    database.increment_stat('successful_chats')
    await context.bot.send_message(chat_id=user1_id, text="✨ Partner found! You can start chatting.", reply_markup=IN_CHAT_REPLY_MARKUP)
    await context.bot.send_message(chat_id=user2_id, text="✨ Partner found! You can start chatting.", reply_markup=IN_CHAT_REPLY_MARKUP)

async def report_partner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def manual_report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def handle_keyboard_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: pass
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

def main() -> None:
    database.initialize_database()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token: logger.error("TELEGRAM_BOT_TOKEN not set."); return
    application = Application.builder().token(token).build()

    # ... (handlers)

    logger.info("Starting bot in polling mode...")
    application.run_polling()

if __name__ == "__main__":
    main()