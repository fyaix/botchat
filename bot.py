import logging
import os
from collections import deque
import asyncio
import time
import re

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Admin and Maintenance ---
ADMIN_IDS = set(int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(',') if admin_id)
maintenance_mode = False
maintenance_message = "🔧 Bot is under maintenance. Please try again later."

# --- Constants ---
DEFAULT_REPUTATION = 80
PREMIUM_REPUTATION = 100
REPUTATION_LEVEL_MONITORED = 79
REPUTATION_LEVEL_RISKY = 59
REPUTATION_LEVEL_SUSPENDED = 40
MAX_VIOLATIONS_BEFORE_SHADOW_BAN = 3

# Behavior constants
SUSPICIOUS_SKIP_TIME_SECONDS = 5
REPUTATION_PENALTY_SUSPICIOUS_MESSAGE = -2
REPUTATION_PENALTY_CONFIRMED_SPAM = -5

# A simple set of forbidden words for content filtering
FORBIDDEN_WORDS = {"spam", "promo", "sale", "vulgarword"}

# --- Data Structures ---
user_profiles = {}
waiting_premium_users = deque()
waiting_users = deque()
active_chats = {}
user_behavior_tracker = {}

# --- Helper Functions ---

def get_or_create_user_profile(user_id: int) -> dict:
    if user_id not in user_profiles:
        is_premium = user_id in ADMIN_IDS # Admins are premium by default

        user_profiles[user_id] = {
            'reputation': PREMIUM_REPUTATION if is_premium else DEFAULT_REPUTATION,
            'is_premium': is_premium,
            'is_trusted': False,
            'is_shadow_banned': False,
            'violation_count': 0,
            'preferences': {'gender': None, 'region': None}
        }
        logger.info(f"Created new profile for user {user_id}.")
    return user_profiles[user_id]

def update_reputation(user_id: int, change: int):
    profile = get_or_create_user_profile(user_id)
    current_reputation = profile['reputation']
    profile['reputation'] = max(0, min(100, current_reputation + change))
    logger.info(f"Reputation for user {user_id} updated from {current_reputation} to {profile['reputation']} (Change: {change}).")

def is_message_suspicious(text: str) -> bool:
    text_lower = text.lower()
    if any(word in text_lower for word in FORBIDDEN_WORDS): return True
    if re.search(r'@\w+', text): return True
    return False

# --- Command Handlers & Core Logic ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    profile = get_or_create_user_profile(user_id)

    if maintenance_mode and user_id not in ADMIN_IDS:
        await update.message.reply_text(maintenance_message)
        return

    if profile['is_shadow_banned']:
        await update.message.reply_text("🔎 Searching for a partner... Please wait.") # Pretend to search
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

    user_behavior_tracker[user_id] = {'last_message_time': None, 'message_sent': False}

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

    if tracker and tracker['message_sent']:
        time_since_message = time.time() - (tracker.get('last_message_time') or 0)
        if time_since_message < SUSPICIOUS_SKIP_TIME_SECONDS:
            logger.warning(f"User {user_id} flagged for suspicious behavior. Triggering human validation for partner {partner_id}.")
            keyboard = [[InlineKeyboardButton("✅ Yes", callback_data=f"validate_yes_{user_id}"), InlineKeyboardButton("❌ No", callback_data=f"validate_no_{user_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(chat_id=partner_id, text="⚠️ Your partner left right after sending a message. Was it a vulgar or promotional message?", reply_markup=reply_markup)
        else:
            update_reputation(user_id, 1)
            update_reputation(partner_id, 1)

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
    profile = get_or_create_user_profile(user_id)

    status_parts = []
    if profile['is_premium']: status_parts.append("Premium")
    if profile['is_trusted']: status_parts.append("Trusted User 🛡️")
    status = ", ".join(status_parts) if status_parts else "Non-Premium"

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

    profile = get_or_create_user_profile(user_id)
    if profile['reputation'] <= REPUTATION_LEVEL_RISKY and update.message.sticker:
        await update.message.reply_text("Your reputation is too low to send stickers.")
        return

    if profile['reputation'] <= REPUTATION_LEVEL_MONITORED:
        await update.message.reply_text("Your message is being sent with a slight delay due to your reputation score.", disable_notification=True)
        await asyncio.sleep(3)

    partner_id = active_chats[user_id]
    message = update.message

    if message.text and is_message_suspicious(message.text):
        update_reputation(user_id, REPUTATION_PENALTY_SUSPICIOUS_MESSAGE)
        await update.message.reply_text("Your message was flagged as suspicious and your reputation has been lowered.", disable_notification=True)
        return

    if user_id in user_behavior_tracker:
        user_behavior_tracker[user_id]['last_message_time'] = time.time()
        user_behavior_tracker[user_id]['message_sent'] = True

    if message.text: await context.bot.send_message(chat_id=partner_id, text=message.text)
    elif message.photo: await context.bot.send_photo(chat_id=partner_id, photo=message.photo[-1].file_id)
    elif message.sticker: await context.bot.send_sticker(chat_id=partner_id, sticker=message.sticker.file_id)
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
    profile = get_or_create_user_profile(reported_user_id)

    if action == "yes":
        update_reputation(reported_user_id, REPUTATION_PENALTY_CONFIRMED_SPAM)
        profile['violation_count'] += 1
        if profile['violation_count'] >= MAX_VIOLATIONS_BEFORE_SHADOW_BAN:
            profile['is_shadow_banned'] = True
            logger.warning(f"User {reported_user_id} has been shadow banned.")
        await query.edit_message_text(text="Thank you. The user has been penalized.")
    else:
        await query.edit_message_text(text="Thank you for your feedback.")

def _check_reciprocal_match(user1_profile: dict, user2_profile: dict) -> bool:
    """Checks if two users' preferences are mutually compatible."""
    # Check if user2 matches user1's preferences
    for key, value in user1_profile['preferences'].items():
        if value is not None and user2_profile.get(key) != value:
            return False
    # Check if user1 matches user2's preferences
    for key, value in user2_profile['preferences'].items():
        if value is not None and user1_profile.get(key) != value:
            return False
    return True

async def try_match_users(context: ContextTypes.DEFAULT_TYPE) -> None:
    """The main matchmaking logic with preference handling."""
    global waiting_users, waiting_premium_users

    # --- Phase 1: Match premium users with preferences ---
    unmatched_premium = deque()
    matched_in_phase1 = set()

    while waiting_premium_users:
        p_user_id = waiting_premium_users.popleft()
        if p_user_id in matched_in_phase1: continue

        p_profile = get_or_create_user_profile(p_user_id)
        has_prefs = any(v is not None for v in p_profile['preferences'].values())

        if not has_prefs:
            unmatched_premium.append(p_user_id)
            continue

        # Search for a preferred partner
        found_match = False
        # Search in other premium users first
        for i, other_p_id in enumerate(list(waiting_premium_users)):
            if _check_reciprocal_match(p_profile, get_or_create_user_profile(other_p_id)):
                await _create_chat(p_user_id, other_p_id, context)
                waiting_premium_users.remove(other_p_id)
                found_match = True
                break

        if not found_match:
            # Search in regular users
            for i, r_user_id in enumerate(list(waiting_users)):
                if _check_reciprocal_match(p_profile, get_or_create_user_profile(r_user_id)):
                    await _create_chat(p_user_id, r_user_id, context)
                    waiting_users.remove(r_user_id)
                    found_match = True
                    break

        if not found_match:
            unmatched_premium.append(p_user_id)

    waiting_premium_users = unmatched_premium

    # --- Phase 2: Match remaining premium users (no pref match found) ---
    while len(waiting_premium_users) >= 2:
        await _create_chat(waiting_premium_users.popleft(), waiting_premium_users.popleft(), context)

    if len(waiting_premium_users) == 1 and len(waiting_users) >= 1:
        await _create_chat(waiting_premium_users.popleft(), waiting_users.popleft(), context)

    # --- Phase 3: Match regular users ---
    while len(waiting_users) >= 2:
        await _create_chat(waiting_users.popleft(), waiting_users.popleft(), context)

async def _create_chat(user1_id: int, user2_id: int, context: ContextTypes.DEFAULT_TYPE):
    active_chats[user1_id] = user2_id
    active_chats[user2_id] = user1_id
    logger.info(f"Matched {user1_id} and {user2_id}.")
    await context.bot.send_message(chat_id=user1_id, text="✅ Partner found! You can start chatting.")
    await context.bot.send_message(chat_id=user2_id, text="✅ Partner found! You can start chatting.")

# --- Premium & Admin Commands ---

async def set_preference(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    profile = get_or_create_user_profile(user_id)

    if not profile['is_premium']:
        await update.message.reply_text("This feature is only available for premium users.")
        return

    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Usage: /set_preference <type> <value>\nExample: /set_preference gender female")
        return

    pref_type, pref_value = args[0].lower(), args[1].lower()
    if pref_type not in profile['preferences']:
        await update.message.reply_text(f"Invalid preference type. Available: {', '.join(profile['preferences'].keys())}")
        return

    # In a real bot, you'd add more validation for pref_value
    profile['preferences'][pref_type] = pref_value
    logger.info(f"User {user_id} set preference {pref_type} to {pref_value}.")
    await update.message.reply_text(f"Preference '{pref_type}' has been set to '{pref_value}'.")

async def maintenance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global maintenance_mode, maintenance_message
    if update.effective_user.id not in ADMIN_IDS: return
    maintenance_mode = True
    custom_message = ' '.join(context.args)
    if custom_message: maintenance_message = custom_message
    logger.info(f"Maintenance mode enabled. Message: {maintenance_message}")
    await update.message.reply_text(f"✅ Maintenance mode enabled.\nMessage: {maintenance_message}")

async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global maintenance_mode
    if update.effective_user.id not in ADMIN_IDS: return
    maintenance_mode = False
    logger.info(f"Maintenance mode disabled.")
    await update.message.reply_text("✅ Bot has been resumed.")

async def shutdown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS: return
    await update.message.reply_text("🛑 Shutting down the bot...")
    logger.info(f"Shutdown command received. Exiting.")
    asyncio.create_task(context.application.shutdown())

async def dashboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays real-time bot statistics."""
    if update.effective_user.id not in ADMIN_IDS: return

    shadow_banned_count = sum(1 for p in user_profiles.values() if p['is_shadow_banned'])

    stats_text = (
        f"📊 *Bot Dashboard*\n\n"
        f"Active Chats: {len(active_chats) // 2}\n"
        f"Waiting Users (Premium): {len(waiting_premium_users)}\n"
        f"Waiting Users (Regular): {len(waiting_users)}\n"
        f"Total Unique Profiles: {len(user_profiles)}\n"
        f"Shadow Banned Users: {shadow_banned_count}"
    )
    await update.message.reply_text(stats_text, parse_mode='MarkdownV2')

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a message to all users who have ever started the bot."""
    if update.effective_user.id not in ADMIN_IDS: return

    message_to_send = ' '.join(context.args)
    if not message_to_send:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    await update.message.reply_text(f"📢 Starting broadcast to {len(user_profiles)} users. This may take a while...")

    success_count = 0
    fail_count = 0
    for user_id in user_profiles.keys():
        try:
            await context.bot.send_message(chat_id=user_id, text=message_to_send)
            success_count += 1
            await asyncio.sleep(0.1) # To avoid rate limiting
        except Exception as e:
            fail_count += 1
            logger.error(f"Failed to send broadcast to {user_id}: {e}")

    await update.message.reply_text(f"Broadcast finished.\n✅ Sent: {success_count}\n❌ Failed: {fail_count}")

async def trust_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Marks a user as trusted."""
    if update.effective_user.id not in ADMIN_IDS: return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /trust_user <user_id>")
        return

    user_id_to_trust = int(context.args[0])
    profile = get_or_create_user_profile(user_id_to_trust)
    profile['is_trusted'] = True

    logger.info(f"Admin {update.effective_user.id} marked user {user_id_to_trust} as trusted.")
    await update.message.reply_text(f"User {user_id_to_trust} has been marked as a Trusted User.")


def main() -> None:
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
    application.add_handler(CommandHandler("set_preference", set_preference))

    # Admin Commands
    application.add_handler(CommandHandler("maintenance", maintenance_cmd))
    application.add_handler(CommandHandler("resume", resume_cmd))
    application.add_handler(CommandHandler("shutdown", shutdown_cmd))
    application.add_handler(CommandHandler("dashboard", dashboard_cmd))
    application.add_handler(CommandHandler("broadcast", broadcast_cmd))
    application.add_handler(CommandHandler("trust_user", trust_user_cmd))

    # Other Handlers
    application.add_handler(CallbackQueryHandler(handle_validation_callback, pattern=r'^validate_'))
    message_handler = MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_message)
    application.add_handler(message_handler)

    application.run_polling()

if __name__ == "__main__":
    main()