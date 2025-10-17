import logging
import os
from collections import deque
import asyncio
import time
import re
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.helpers import escape_markdown

import database

# Load environment variables from .env file
load_dotenv()

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Admin ---
ADMIN_IDS = set(int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(',') if admin_id)

# --- Constants ---
REPUTATION_LEVEL_MONITORED = 79
REPUTATION_LEVEL_RISKY = 59
REPUTATION_LEVEL_SUSPENDED = 40
MAX_VIOLATIONS_BEFORE_SHADOW_BAN = 3
SUSPICIOUS_SKIP_TIME_SECONDS = 5

# Reputation change values from the design document
REP_CHANGE_NORMAL_CHAT = 5
REP_CHANGE_REPORTED_ONCE = -3
REP_CHANGE_SUSPICIOUS_MESSAGE = -2
REP_CHANGE_CONFIRMED_SPAM = -5
REP_CHANGE_FALSE_REPORT = -2

FORBIDDEN_WORDS = {"spam", "promo", "sale", "vulgarword"}

# --- In-memory Session Data ---
waiting_premium_users = deque()
waiting_users = deque()
active_chats = {}
user_behavior_tracker = {}

# --- Helper Functions ---

def is_message_suspicious(text: str) -> bool:
    text_lower = text.lower()
    if any(word in text_lower for word in FORBIDDEN_WORDS): return True
    if re.search(r'@\w+', text): return True
    return False

# --- Command Handlers & Core Logic ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    profile = database.get_or_create_user_profile(user_id, admin_ids=ADMIN_IDS)

    if profile['is_shadow_banned']:
        await update.message.reply_text("🔎 Searching for a partner... Please wait.")
        logger.warning(f"Shadow-banned user {user_id} attempted to start a chat.")
        return

    if profile['reputation'] < REPUTATION_LEVEL_SUSPENDED:
        await update.message.reply_text("Your reputation is too low to start a new chat.")
        return

    if user_id in active_chats:
        await update.message.reply_text("You are already in a chat. Use /stop to end it.")
        return

    if user_id in waiting_users or user_id in waiting_premium_users:
        await update.message.reply_text("You are already looking for a partner. Please wait.")
        return

    user_behavior_tracker[user_id] = {
        'last_message_time': None,
        'message_sent': False,
        'start_time': time.time(),
        'delay_notified': False # Flag for one-time notification
    }

    if profile['is_premium']:
        waiting_premium_users.append(user_id)
        await update.message.reply_text("👑 Searching for a partner with high priority... Please wait.")
    else:
        waiting_users.append(user_id)
        await update.message.reply_text("🔎 Searching for a partner... Please wait.")

    logger.info(f"User {user_id} (Rep: {profile['reputation']}) started searching.")
    await try_match_users(context)

async def handle_chat_disconnection(user_id: int, context: ContextTypes.DEFAULT_TYPE, is_next: bool = False):
    if user_id not in active_chats:
        if not is_next: await context.bot.send_message(chat_id=user_id, text="You are not in a chat. Use /start to find one.")
        return False

    partner_id = active_chats[user_id]
    tracker = user_behavior_tracker.get(user_id)

    if tracker:
        chat_duration = time.time() - tracker.get('start_time', 0)
        message_sent = tracker.get('message_sent', False)

        if message_sent and chat_duration < SUSPICIOUS_SKIP_TIME_SECONDS:
            logger.warning(f"User {user_id} flagged for suspicious behavior. Triggering human validation for partner {partner_id}.")
            keyboard = [[InlineKeyboardButton("✅ Yes", callback_data=f"validate_yes_{user_id}"), InlineKeyboardButton("❌ No", callback_data=f"validate_no_{user_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(chat_id=partner_id, text="⚠️ Your partner left right after sending a message. Was it a vulgar or promotional message?", reply_markup=reply_markup)
        elif chat_duration > 60: # Normal chat > 1 min
            profile = database.get_user_profile(user_id)
            database.update_user_profile(user_id, {'reputation': profile['reputation'] + REP_CHANGE_NORMAL_CHAT})
            partner_profile = database.get_user_profile(partner_id)
            database.update_user_profile(partner_id, {'reputation': partner_profile['reputation'] + REP_CHANGE_NORMAL_CHAT})

    if not is_next: await context.bot.send_message(chat_id=user_id, text="💬 You have left the chat.")
    await context.bot.send_message(chat_id=partner_id, text="💬 Your partner has left the chat.")

    del active_chats[user_id]
    del active_chats[partner_id]

    if user_id in user_behavior_tracker: del user_behavior_tracker[user_id]
    if partner_id in user_behavior_tracker: del user_behavior_tracker[partner_id]

    logger.info(f"Chat ended between {user_id} and {partner_id}.")
    return True

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_chat_disconnection(update.effective_user.id, context)

async def next_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if await handle_chat_disconnection(user_id, context, is_next=True):
        await start(update, context)

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    profile = database.get_or_create_user_profile(user_id, admin_ids=ADMIN_IDS)

    status_parts = []
    if profile['is_premium']: status_parts.append("Premium")
    if profile['is_trusted']: status_parts.append("Trusted User 🛡️")
    raw_status = ", ".join(status_parts) if status_parts else "Non-Premium"
    status = escape_markdown(raw_status, version=2)

    shadow_banned_status = "Yes" if profile['is_shadow_banned'] else "No"

    profile_text = (
        f"👤 *Your Profile*\n"
        f"ID: `{user_id}`\n"
        f"Status: {status}\n"
        f"Reputation: {profile['reputation']}\n"
        f"Violations: {profile['violation_count']}\n"
        f"Shadow Banned: {shadow_banned_status}"
    )
    await update.message.reply_text(profile_text, parse_mode='MarkdownV2')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in active_chats:
        await update.message.reply_text("You are not in a chat. Use /start to find one.")
        return

    profile = database.get_or_create_user_profile(user_id, admin_ids=ADMIN_IDS)
    if profile['reputation'] <= REPUTATION_LEVEL_RISKY and update.message.sticker:
        await update.message.reply_text("Your reputation is too low to send stickers.")
        return

    if profile['reputation'] <= REPUTATION_LEVEL_MONITORED:
        tracker = user_behavior_tracker.get(user_id, {})
        if not tracker.get('delay_notified', False):
            await update.message.reply_text("Your messages are being sent with a slight delay due to your low reputation score.", disable_notification=True)
            tracker['delay_notified'] = True
        await asyncio.sleep(3)

    partner_id = active_chats[user_id]
    message = update.message

    if message.text and is_message_suspicious(message.text):
        new_rep = profile['reputation'] + REP_CHANGE_SUSPICIOUS_MESSAGE
        database.update_user_profile(user_id, {'reputation': new_rep})
        await update.message.reply_text("Your message was flagged as suspicious and your reputation has been lowered.", disable_notification=True)
        return

    if user_id in user_behavior_tracker:
        user_behavior_tracker[user_id]['message_sent'] = True
        user_behavior_tracker[user_id]['last_message_time'] = time.time()

    if message.text: await context.bot.send_message(chat_id=partner_id, text=message.text)
    elif message.photo: await context.bot.send_photo(chat_id=partner_id, photo=message.photo[-1].file_id)
    elif message.sticker:
        if database.is_sticker_banned(message.sticker.file_unique_id):
            await update.message.reply_text("This sticker is not allowed.")
            new_rep = profile['reputation'] + REP_CHANGE_SUSPICIOUS_MESSAGE
            database.update_user_profile(user_id, {'reputation': new_rep})
            return
        await context.bot.send_sticker(chat_id=partner_id, sticker=message.sticker.file_id)
    elif message.voice: await context.bot.send_voice(chat_id=partner_id, voice=message.voice.file_id)
    elif message.video: await context.bot.send_video(chat_id=partner_id, video=message.video.file_id)
    elif message.document: await context.bot.send_document(chat_id=partner_id, document=message.document.file_id)
    else: await update.message.reply_text("Unsupported message type.")

async def handle_validation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data.split('_')
    action, reported_user_id_str = data[1], data[2]
    reported_user_id = int(reported_user_id_str)
    profile = database.get_or_create_user_profile(reported_user_id, admin_ids=ADMIN_IDS)

    if action == "yes":
        updates = {
            'reputation': profile['reputation'] + REP_CHANGE_CONFIRMED_SPAM,
            'violation_count': profile['violation_count'] + 1
        }
        if updates['violation_count'] >= MAX_VIOLATIONS_BEFORE_SHADOW_BAN:
            updates['is_shadow_banned'] = True
            logger.warning(f"User {reported_user_id} has been shadow banned.")
        database.update_user_profile(reported_user_id, updates)
        await query.edit_message_text(text="Thank you. The user has been penalized.")
    else: # The report was false
        reporter_user_id = query.from_user.id
        reporter_profile = database.get_or_create_user_profile(reporter_user_id, admin_ids=ADMIN_IDS)
        new_rep = reporter_profile['reputation'] + REP_CHANGE_FALSE_REPORT
        database.update_user_profile(reporter_user_id, {'reputation': new_rep})
        logger.info(f"User {reporter_user_id} penalized for a false report.")
        await query.edit_message_text(text="Thank you for your feedback. No action was taken against the other user.")

def _check_reciprocal_match(user1_profile: dict, user2_profile: dict) -> bool:
    # Check gender and region
    for key in ['gender', 'region']:
        if user1_profile['preferences'].get(key) is not None and user1_profile['preferences'][key] != user2_profile.get(key):
            return False
        if user2_profile['preferences'].get(key) is not None and user2_profile['preferences'][key] != user1_profile.get(key):
            return False

    # Check age preference
    u1_age_pref = user1_profile['preferences'].get('age')
    u2_age_pref = user2_profile['preferences'].get('age')
    u1_age = user1_profile.get('age')
    u2_age = user2_profile.get('age')

    if u1_age_pref is not None and u2_age is not None:
        if abs(u1_age_pref - u2_age) > 5: # Example: allow 5-year age difference
            return False
    if u2_age_pref is not None and u1_age is not None:
        if abs(u2_age_pref - u1_age) > 5:
            return False

    return True

async def try_match_users(context: ContextTypes.DEFAULT_TYPE) -> None:
    global waiting_users, waiting_premium_users
    unmatched_premium = deque()
    matched_in_phase1 = set()

    while waiting_premium_users:
        p_user_id = waiting_premium_users.popleft()
        if p_user_id in matched_in_phase1: continue
        p_profile = database.get_user_profile(p_user_id)
        if p_profile['is_shadow_banned']: continue
        has_prefs = any(v is not None for v in p_profile['preferences'].values())
        if not has_prefs:
            unmatched_premium.append(p_user_id)
            continue
        found_match = False
        potential_partners = list(waiting_premium_users) + list(waiting_users)
        for partner_id in potential_partners:
            partner_profile = database.get_user_profile(partner_id)
            if _check_reciprocal_match(p_profile, partner_profile):
                await _create_chat(p_user_id, partner_id, context)
                if partner_id in waiting_premium_users: waiting_premium_users.remove(partner_id)
                else: waiting_users.remove(partner_id)
                found_match = True
                break
        if not found_match:
            unmatched_premium.append(p_user_id)
    waiting_premium_users = unmatched_premium
    while len(waiting_premium_users) >= 2:
        await _create_chat(waiting_premium_users.popleft(), waiting_premium_users.popleft(), context)
    if len(waiting_premium_users) == 1 and len(waiting_users) >= 1:
        await _create_chat(waiting_premium_users.popleft(), waiting_users.popleft(), context)
    while len(waiting_users) >= 2:
        await _create_chat(waiting_users.popleft(), waiting_users.popleft(), context)

async def _create_chat(user1_id: int, user2_id: int, context: ContextTypes.DEFAULT_TYPE):
    active_chats[user1_id] = user2_id
    active_chats[user2_id] = user1_id
    user_behavior_tracker[user1_id] = {'last_message_time': None, 'message_sent': False, 'start_time': time.time(), 'delay_notified': False}
    user_behavior_tracker[user2_id] = {'last_message_time': None, 'message_sent': False, 'start_time': time.time(), 'delay_notified': False}
    logger.info(f"Matched {user1_id} and {user2_id}.")
    await context.bot.send_message(chat_id=user1_id, text="✅ Partner found! You can start chatting.")
    await context.bot.send_message(chat_id=user2_id, text="✅ Partner found! You can start chatting.")

# --- Premium & Admin Commands ---
async def setpreference(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    profile = database.get_or_create_user_profile(user_id, admin_ids=ADMIN_IDS)
    if not profile['is_premium']:
        await update.message.reply_text("This feature is only available for premium users.")
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Usage: /setpreference <type> <value>\nExample: /setpreference gender female or /setpreference age 25")
        return
    pref_type, pref_value = args[0].lower(), args[1].lower()
    if pref_type not in profile['preferences']:
        await update.message.reply_text(f"Invalid preference type. Available: {', '.join(profile['preferences'].keys())}")
        return

    if pref_type == 'age':
        if not pref_value.isdigit():
            await update.message.reply_text("Age must be a number.")
            return
        pref_value = int(pref_value)

    new_prefs = profile['preferences']
    new_prefs[pref_type] = pref_value
    database.update_user_profile(user_id, {'preferences': new_prefs})
    logger.info(f"User {user_id} set preference {pref_type} to {pref_value}.")
    await update.message.reply_text(f"Preference '{pref_type}' has been set to '{pref_value}'.")

async def maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS: return
    message_to_send = ' '.join(context.args)
    if not message_to_send:
        await update.message.reply_text("Usage: /maintenance <announcement_message>")
        return
    full_message = f"🔧 **Maintenance Announcement** 🔧\n\n{message_to_send}"
    await update.message.reply_text(f"📢 Starting maintenance broadcast to {len(database.get_all_user_ids())} users...")
    success_count, fail_count = 0, 0
    for user_id in database.get_all_user_ids():
        try:
            await context.bot.send_message(chat_id=user_id, text=full_message, parse_mode='Markdown')
            success_count += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            fail_count += 1
            logger.error(f"Failed to send maintenance broadcast to {user_id}: {e}")
    await update.message.reply_text(f"Maintenance broadcast finished.\n✅ Sent: {success_count}\n❌ Failed: {fail_count}")

async def shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS: return
    await update.message.reply_text("🛑 Shutting down the bot...")
    logger.info(f"Shutdown command received. Exiting.")
    asyncio.create_task(context.application.shutdown())

async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS: return
    all_user_ids = database.get_all_user_ids()
    all_profiles = [database.get_user_profile(uid) for uid in all_user_ids]
    shadow_banned_count = sum(1 for p in all_profiles if p and p['is_shadow_banned'])
    stats_text = (
        f"📊 *Bot Dashboard*\n\n"
        f"Active Chats: {len(active_chats) // 2}\n"
        f"Waiting Users (Premium): {len(waiting_premium_users)}\n"
        f"Waiting Users (Regular): {len(waiting_users)}\n"
        f"Total Unique Profiles: {len(all_user_ids)}\n"
        f"Shadow Banned Users: {shadow_banned_count}"
    )
    await update.message.reply_text(stats_text, parse_mode='MarkdownV2')

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS: return
    message_to_send = ' '.join(context.args)
    if not message_to_send:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    user_ids = database.get_all_user_ids()
    await update.message.reply_text(f"📢 Starting broadcast to {len(user_ids)} users. This may take a while...")
    success_count, fail_count = 0, 0
    for user_id in user_ids:
        try:
            await context.bot.send_message(chat_id=user_id, text=message_to_send)
            success_count += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            fail_count += 1
            logger.error(f"Failed to send broadcast to {user_id}: {e}")
    await update.message.reply_text(f"Broadcast finished.\n✅ Sent: {success_count}\n❌ Failed: {fail_count}")

async def trustuser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS: return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /trustuser <user_id>")
        return
    user_id_to_trust = int(context.args[0])
    database.update_user_profile(user_id_to_trust, {'is_trusted': True})
    logger.info(f"Admin {update.effective_user.id} marked user {user_id_to_trust} as trusted.")
    await update.message.reply_text(f"User {user_id_to_trust} has been marked as a Trusted User.")

async def bansticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bans a sticker by replying to it with this command."""
    if update.effective_user.id not in ADMIN_IDS: return
    if not update.message.reply_to_message or not update.message.reply_to_message.sticker:
        await update.message.reply_text("Please reply to a sticker to ban it.")
        return
    sticker = update.message.reply_to_message.sticker
    database.add_banned_sticker(sticker.file_unique_id, sticker.file_id)
    await update.message.reply_text("Sticker has been banned successfully.")

async def listbannedstickers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists all banned stickers by sending them."""
    if update.effective_user.id not in ADMIN_IDS: return

    banned_stickers = database.get_all_banned_stickers()
    if not banned_stickers:
        await update.message.reply_text("There are no banned stickers.")
        return

    await update.message.reply_text("📋 **Banned Stickers:**")
    for sticker in banned_stickers:
        try:
            await context.bot.send_sticker(chat_id=update.effective_chat.id, sticker=sticker['sticker_file_id'])
        except Exception as e:
            await update.message.reply_text(f"Could not send sticker with unique_id: `{sticker['sticker_unique_id']}`\nError: {e}", parse_mode='MarkdownV2')

async def unbansticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unbans a sticker by replying to it with this command."""
    if update.effective_user.id not in ADMIN_IDS: return

    if not update.message.reply_to_message or not update.message.reply_to_message.sticker:
        await update.message.reply_text("Please reply to a sticker to unban it.")
        return

    sticker_id = update.message.reply_to_message.sticker.file_unique_id
    database.remove_banned_sticker(sticker_id)
    await update.message.reply_text("Sticker has been unbanned successfully.")

def main() -> None:
    database.initialize_database()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable not set.")
        return
    application = Application.builder().token(token).build()

    # Command Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("next", next_chat))
    application.add_handler(CommandHandler("myid", myid))
    application.add_handler(CommandHandler("setpreference", setpreference))

    # Admin Commands
    application.add_handler(CommandHandler("maintenance", maintenance))
    application.add_handler(CommandHandler("shutdown", shutdown))
    application.add_handler(CommandHandler("dashboard", dashboard))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("trustuser", trustuser))
    application.add_handler(CommandHandler("bansticker", bansticker))
    application.add_handler(CommandHandler("unbansticker", unbansticker))
    application.add_handler(CommandHandler("listbannedstickers", listbannedstickers))

    # Other Handlers
    application.add_handler(CallbackQueryHandler(handle_validation_callback, pattern=r'^validate_'))
    message_handler = MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_message)
    application.add_handler(message_handler)

    logger.info("Starting bot in polling mode...")
    application.run_polling()

if __name__ == "__main__":
    main()