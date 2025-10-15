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

# --- Data Structures ---
waiting_users = deque()
active_chats = {}
user_reputations = {}
user_behavior_tracker = {}

# --- Constants ---
DEFAULT_REPUTATION = 80
PREMIUM_REPUTATION = 100
REPUTATION_LEVEL_MONITORED = 79
REPUTATION_LEVEL_RISKY = 59
REPUTATION_LEVEL_SUSPENDED = 40

# Behavior constants
SUSPICIOUS_SKIP_TIME_SECONDS = 5
REPUTATION_PENALTY_SUSPICIOUS_MESSAGE = -2
REPUTATION_PENALTY_SUSPICIOUS_BEHAVIOR = -3
REPUTATION_PENALTY_CONFIRMED_SPAM = -5

# A simple set of forbidden words for content filtering
FORBIDDEN_WORDS = {"spam", "promo", "sale", "vulgarword"} # Example words

# --- Helper Functions ---

def get_user_reputation(user_id: int) -> int:
    if user_id not in user_reputations:
        user_reputations[user_id] = DEFAULT_REPUTATION
    return user_reputations.get(user_id, DEFAULT_REPUTATION)

def update_reputation(user_id: int, change: int):
    current_reputation = get_user_reputation(user_id)
    new_reputation = max(0, min(100, current_reputation + change))
    user_reputations[user_id] = new_reputation
    logger.info(f"Reputation for user {user_id} updated from {current_reputation} to {new_reputation} (Change: {change}).")

def is_message_suspicious(text: str) -> bool:
    text_lower = text.lower()
    if any(word in text_lower for word in FORBIDDEN_WORDS):
        return True
    if re.search(r'@\w+', text):
        return True
    return False

# --- Command Handlers & Core Logic ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    reputation = get_user_reputation(user_id)

    if reputation < REPUTATION_LEVEL_SUSPENDED:
        await update.message.reply_text("Your reputation is too low to start a new chat.")
        return

    if user_id in active_chats:
        await update.message.reply_text("You are already in a chat. Use /stop to end it.")
        return

    if user_id in waiting_users:
        await update.message.reply_text("You are already looking for a partner. Please wait.")
        return

    user_behavior_tracker[user_id] = {'last_message_time': None, 'message_sent': False}
    waiting_users.append(user_id)
    await update.message.reply_text("🔎 Searching for a partner... Please wait.")
    logger.info(f"User {user_id} (Rep: {reputation}) started searching.")
    await try_match_users(context)

async def handle_chat_disconnection(user_id: int, context: ContextTypes.DEFAULT_TYPE, is_next: bool = False):
    if user_id not in active_chats:
        if not is_next: # Avoid duplicate messages on /next
            await context.bot.send_message(chat_id=user_id, text="You are not in a chat. Use /start to find one.")
        return False

    partner_id = active_chats[user_id]

    tracker = user_behavior_tracker.get(user_id)
    if tracker and tracker['message_sent']:
        time_since_message = time.time() - (tracker.get('last_message_time') or 0)
        if time_since_message < SUSPICIOUS_SKIP_TIME_SECONDS:
            logger.warning(f"User {user_id} flagged for suspicious behavior. Triggering human validation for partner {partner_id}.")
            keyboard = [
                [
                    InlineKeyboardButton("✅ Yes", callback_data=f"validate_yes_{user_id}"),
                    InlineKeyboardButton("❌ No", callback_data=f"validate_no_{user_id}"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(
                chat_id=partner_id,
                text="⚠️ Your partner left right after sending a message. Was it a vulgar or promotional message?",
                reply_markup=reply_markup
            )
        else:
            # Normal chat ending > 1 min can increase reputation
            update_reputation(user_id, 1) # Small reward
            update_reputation(partner_id, 1)

    if not is_next:
        await context.bot.send_message(chat_id=user_id, text="💬 You have left the chat.")
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
    reputation = get_user_reputation(user_id)
    await update.message.reply_text(f"Your unique ID is: `{user_id}`\nYour reputation score is: {reputation}", parse_mode='MarkdownV2')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    reputation = get_user_reputation(user_id)

    if user_id not in active_chats:
        await update.message.reply_text("You are not in a chat. Use /start to find one.")
        return

    if reputation <= REPUTATION_LEVEL_RISKY and update.message.sticker:
        await update.message.reply_text("Your reputation is too low to send stickers.")
        return

    if reputation <= REPUTATION_LEVEL_MONITORED:
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

# --- Callback Query Handler ---

async def handle_validation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the user's response to the validation query."""
    query = update.callback_query
    await query.answer()

    data = query.data.split('_')
    action = data[1]
    reported_user_id = int(data[2])

    if action == "yes":
        update_reputation(reported_user_id, REPUTATION_PENALTY_CONFIRMED_SPAM)
        await query.edit_message_text(text="Thank you for your feedback. The user has been penalized.")
    else: # "no"
        await query.edit_message_text(text="Thank you for your feedback. No action will be taken.")

# --- Main Bot Logic ---

async def try_match_users(context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(waiting_users) >= 2:
        user1_id = waiting_users.popleft()
        user2_id = waiting_users.popleft()

        active_chats[user1_id] = user2_id
        active_chats[user2_id] = user1_id

        logger.info(f"Matched {user1_id} and {user2_id}.")
        await context.bot.send_message(chat_id=user1_id, text="✅ Partner found! You can start chatting.")
        await context.bot.send_message(chat_id=user2_id, text="✅ Partner found! You can start chatting.")

def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable not set.")
        return

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("next", next_chat))
    application.add_handler(CommandHandler("myid", myid))

    application.add_handler(CallbackQueryHandler(handle_validation_callback, pattern=r'^validate_'))

    message_handlers = [
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message),
        MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_message),
        MessageHandler(filters.STICKER & ~filters.COMMAND, handle_message),
        MessageHandler(filters.VOICE & ~filters.COMMAND, handle_message),
        MessageHandler(filters.VIDEO & ~filters.COMMAND, handle_message),
        MessageHandler(filters.DOCUMENT & ~filters.COMMAND, handle_message)
    ]
    for handler in message_handlers:
        application.add_handler(handler)

    application.run_polling()

if __name__ == "__main__":
    main()