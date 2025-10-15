import logging
import os
from collections import deque

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Data Structures ---
# A queue for users waiting to be matched
waiting_users = deque()

# A dictionary to store active chats: {user_id: partner_id}
active_chats = {}

# --- Command Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts a chat by adding the user to the waiting queue."""
    user_id = update.effective_user.id

    if user_id in active_chats:
        await update.message.reply_text("You are already in a chat. Use /stop to end it.")
        return

    if user_id in waiting_users:
        await update.message.reply_text("You are already looking for a partner. Please wait.")
        return

    # Add user to the waiting queue
    waiting_users.append(user_id)
    await update.message.reply_text("🔎 Searching for a partner... Please wait.")
    logger.info(f"User {user_id} started searching.")

    # Try to match with another user
    await try_match_users(context)


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stops the current chat."""
    user_id = update.effective_user.id

    if user_id not in active_chats:
        await update.message.reply_text("You are not in a chat. Use /start to find one.")
        return

    partner_id = active_chats[user_id]

    # Notify both users
    await context.bot.send_message(chat_id=user_id, text="💬 You have left the chat.")
    await context.bot.send_message(chat_id=partner_id, text="💬 Your partner has left the chat.")

    # Remove chat from active_chats
    del active_chats[user_id]
    del active_chats[partner_id]

    logger.info(f"Chat ended between {user_id} and {partner_id}.")


async def next_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stops the current chat and finds a new one."""
    user_id = update.effective_user.id

    if user_id not in active_chats:
        await update.message.reply_text("You are not in a chat. Use /start to find one.")
        return

    # First, stop the current chat
    await stop(update, context)

    # Then, start searching for a new one
    await start(update, context)


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the user's unique chat ID."""
    user_id = update.effective_user.id
    await update.message.reply_text(f"Your unique ID is: `{user_id}`", parse_mode='MarkdownV2')


# --- Message Handler ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles messages and forwards them to the chat partner."""
    user_id = update.effective_user.id

    if user_id not in active_chats:
        await update.message.reply_text("You are not in a chat. Use /start to find one.")
        return

    partner_id = active_chats[user_id]
    message = update.message

    # Forward the message content
    if message.text:
        await context.bot.send_message(chat_id=partner_id, text=message.text)
    elif message.photo:
        await context.bot.send_photo(chat_id=partner_id, photo=message.photo[-1].file_id)
    elif message.sticker:
        await context.bot.send_sticker(chat_id=partner_id, sticker=message.sticker.file_id)
    elif message.voice:
        await context.bot.send_voice(chat_id=partner_id, voice=message.voice.file_id)
    elif message.video:
        await context.bot.send_video(chat_id=partner_id, video=message.video.file_id)
    elif message.document:
        await context.bot.send_document(chat_id=partner_id, document=message.document.file_id)
    else:
        await update.message.reply_text("Unsupported message type.")


# --- Helper Functions ---

async def try_match_users(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tries to match two users from the waiting queue."""
    if len(waiting_users) >= 2:
        user1_id = waiting_users.popleft()
        user2_id = waiting_users.popleft()

        # Create the chat connection
        active_chats[user1_id] = user2_id
        active_chats[user2_id] = user1_id

        logger.info(f"Matched {user1_id} and {user2_id}.")

        # Notify both users
        await context.bot.send_message(chat_id=user1_id, text="✅ Partner found! You can start chatting.")
        await context.bot.send_message(chat_id=user2_id, text="✅ Partner found! You can start chatting.")


def main() -> None:
    """Run the bot."""
    # Get the token from environment variable
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable not set.")
        return

    # Create the Application and pass it your bot's token.
    application = Application.builder().token(token).build()

    # on different commands - answer in Telegram
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("next", next_chat))
    application.add_handler(CommandHandler("myid", myid))

    # on non command i.e message - forward the message
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.STICKER & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.VIDEO & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.DOCUMENT & ~filters.COMMAND, handle_message))

    # Run the bot until the user presses Ctrl-C
    application.run_polling()


if __name__ == "__main__":
    main()