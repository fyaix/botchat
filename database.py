import sqlite3
import logging
import json

DATABASE_FILE = "bot.db"
logger = logging.getLogger(__name__)

def initialize_database():
    try:
        con = sqlite3.connect(DATABASE_FILE)
        cur = con.cursor()
        # User profiles table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                reputation INTEGER NOT NULL,
                is_premium BOOLEAN NOT NULL DEFAULT 0,
                is_trusted BOOLEAN NOT NULL DEFAULT 0,
                is_shadow_banned BOOLEAN NOT NULL DEFAULT 0,
                violation_count INTEGER NOT NULL DEFAULT 0,
                preferences TEXT,
                age INTEGER,
                gender TEXT
            )
        """)
        # Banned stickers table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS banned_stickers (
                sticker_unique_id TEXT PRIMARY KEY,
                sticker_file_id TEXT NOT NULL
            )
        """)
        # Global stats table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_stats (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            )
        """)
        # Initialize total_reports if not exists
        cur.execute("INSERT OR IGNORE INTO bot_stats (key, value) VALUES ('total_reports', 0)")
        con.commit()
        con.close()
        logger.info("Database initialized successfully.")
    except sqlite3.Error as e:
        logger.error(f"Database error on initialization: {e}", exc_info=True)
        raise

def dict_factory(cursor, row):
    fields = [column[0] for column in cursor.description]
    return {key: value for key, value in zip(fields, row)}

def get_user_profile(user_id: int) -> dict | None:
    try:
        con = sqlite3.connect(DATABASE_FILE); con.row_factory = dict_factory; cur = con.cursor()
        res = cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        profile = res.fetchone()
        con.close()
        if profile and profile.get('preferences'):
            profile['preferences'] = json.loads(profile['preferences'])
        return profile
    except sqlite3.Error as e:
        logger.error(f"DB error getting profile for {user_id}: {e}", exc_info=True)
        return None

def create_user_profile(user_id: int, is_premium: bool = False) -> dict:
    profile = {'user_id': user_id, 'reputation': 100 if is_premium else 80, 'is_premium': is_premium,
               'is_trusted': False, 'is_shadow_banned': False, 'violation_count': 0,
               'preferences': json.dumps({'gender': None, 'region': None, 'age': None}), 'age': None, 'gender': None}
    try:
        con = sqlite3.connect(DATABASE_FILE); cur = con.cursor()
        cur.execute("INSERT INTO users (user_id, reputation, is_premium, is_trusted, is_shadow_banned, violation_count, preferences, age, gender) "
                    "VALUES (:user_id, :reputation, :is_premium, :is_trusted, :is_shadow_banned, :violation_count, :preferences, :age, :gender)", profile)
        con.commit(); con.close()
        profile['preferences'] = json.loads(profile['preferences'])
        return profile
    except sqlite3.Error as e:
        logger.error(f"DB error creating profile for {user_id}: {e}", exc_info=True)
        raise

def get_or_create_user_profile(user_id: int, admin_ids: set) -> dict:
    profile = get_user_profile(user_id)
    if not profile:
        is_premium = user_id in admin_ids
        profile = create_user_profile(user_id, is_premium=is_premium)
    return profile

def update_user_profile(user_id: int, updates: dict):
    if 'preferences' in updates: updates['preferences'] = json.dumps(updates['preferences'])
    fields = ", ".join([f"{key} = ?" for key in updates.keys()])
    values = list(updates.values()); values.append(user_id)
    try:
        con = sqlite3.connect(DATABASE_FILE); cur = con.cursor()
        cur.execute(f"UPDATE users SET {fields} WHERE user_id = ?", tuple(values)); con.commit(); con.close()
    except sqlite3.Error as e:
        logger.error(f"DB error updating profile for {user_id}: {e}", exc_info=True)

def get_all_user_ids() -> list[int]:
    try:
        con = sqlite3.connect(DATABASE_FILE); cur = con.cursor()
        res = cur.execute("SELECT user_id FROM users"); user_ids = [row[0] for row in res.fetchall()]; con.close()
        return user_ids
    except sqlite3.Error as e:
        logger.error(f"DB error getting all user IDs: {e}", exc_info=True); return []

# --- Sticker Management ---
def add_banned_sticker(sticker_unique_id: str, sticker_file_id: str):
    try:
        con = sqlite3.connect(DATABASE_FILE); cur = con.cursor()
        cur.execute("INSERT OR REPLACE INTO banned_stickers (sticker_unique_id, sticker_file_id) VALUES (?, ?)", (sticker_unique_id, sticker_file_id)); con.commit(); con.close()
    except sqlite3.Error as e:
        logger.error(f"DB error banning sticker {sticker_unique_id}: {e}", exc_info=True)

def remove_banned_sticker(sticker_unique_id: str):
    try:
        con = sqlite3.connect(DATABASE_FILE); cur = con.cursor()
        cur.execute("DELETE FROM banned_stickers WHERE sticker_unique_id = ?", (sticker_unique_id,)); con.commit(); con.close()
    except sqlite3.Error as e:
        logger.error(f"DB error unbanning sticker {sticker_unique_id}: {e}", exc_info=True)

def is_sticker_banned(sticker_unique_id: str) -> bool:
    try:
        con = sqlite3.connect(DATABASE_FILE); cur = con.cursor()
        res = cur.execute("SELECT 1 FROM banned_stickers WHERE sticker_unique_id = ?", (sticker_unique_id,)); is_banned = res.fetchone() is not None; con.close()
        return is_banned
    except sqlite3.Error as e:
        logger.error(f"DB error checking sticker {sticker_unique_id}: {e}", exc_info=True); return False

def get_all_banned_stickers() -> list[dict]:
    try:
        con = sqlite3.connect(DATABASE_FILE); con.row_factory = dict_factory; cur = con.cursor()
        res = cur.execute("SELECT sticker_unique_id, sticker_file_id FROM banned_stickers"); stickers = res.fetchall(); con.close()
        return stickers
    except sqlite3.Error as e:
        logger.error(f"DB error getting all banned stickers: {e}", exc_info=True); return []

# --- Stats Management ---
def increment_total_reports():
    try:
        con = sqlite3.connect(DATABASE_FILE); cur = con.cursor()
        cur.execute("UPDATE bot_stats SET value = value + 1 WHERE key = 'total_reports'"); con.commit(); con.close()
    except sqlite3.Error as e:
        logger.error(f"DB error incrementing total reports: {e}", exc_info=True)

def get_total_reports() -> int:
    try:
        con = sqlite3.connect(DATABASE_FILE); cur = con.cursor()
        res = cur.execute("SELECT value FROM bot_stats WHERE key = 'total_reports'"); total = res.fetchone(); con.close()
        return total[0] if total else 0
    except sqlite3.Error as e:
        logger.error(f"DB error getting total reports: {e}", exc_info=True); return 0