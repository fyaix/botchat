import sqlite3
import logging
import json

DATABASE_FILE = "bot.db"

# --- Constants for new profiles ---
DEFAULT_REPUTATION = 80
PREMIUM_REPUTATION = 100

logger = logging.getLogger(__name__)

def initialize_database():
    """Creates the database and the users table if they don't exist."""
    try:
        con = sqlite3.connect(DATABASE_FILE)
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                reputation INTEGER NOT NULL,
                is_premium BOOLEAN NOT NULL DEFAULT 0,
                is_trusted BOOLEAN NOT NULL DEFAULT 0,
                is_shadow_banned BOOLEAN NOT NULL DEFAULT 0,
                violation_count INTEGER NOT NULL DEFAULT 0,
                preferences TEXT
            )
        """)
        con.commit()
        con.close()
        logger.info("Database initialized successfully.")
    except sqlite3.Error as e:
        logger.error(f"Database error on initialization: {e}")
        raise

def dict_factory(cursor, row):
    """Converts database query results into a dictionary."""
    fields = [column[0] for column in cursor.description]
    return {key: value for key, value in zip(fields, row)}

def get_user_profile(user_id: int) -> dict | None:
    """Retrieves a user profile from the database."""
    try:
        con = sqlite3.connect(DATABASE_FILE)
        con.row_factory = dict_factory
        cur = con.cursor()
        res = cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        profile = res.fetchone()
        con.close()
        if profile and profile.get('preferences'):
            profile['preferences'] = json.loads(profile['preferences'])
        return profile
    except sqlite3.Error as e:
        logger.error(f"Database error getting profile for {user_id}: {e}")
        return None

def create_user_profile(user_id: int, is_premium: bool = False) -> dict:
    """Creates a new user profile in the database."""
    profile = {
        'user_id': user_id,
        'reputation': PREMIUM_REPUTATION if is_premium else DEFAULT_REPUTATION,
        'is_premium': is_premium,
        'is_trusted': False,
        'is_shadow_banned': False,
        'violation_count': 0,
        'preferences': json.dumps({'gender': None, 'region': None})
    }
    try:
        con = sqlite3.connect(DATABASE_FILE)
        cur = con.cursor()
        cur.execute("""
            INSERT INTO users (user_id, reputation, is_premium, is_trusted, is_shadow_banned, violation_count, preferences)
            VALUES (:user_id, :reputation, :is_premium, :is_trusted, :is_shadow_banned, :violation_count, :preferences)
        """, profile)
        con.commit()
        con.close()
        logger.info(f"Created new profile for user {user_id} in database.")
        # Convert preferences back to dict for return
        profile['preferences'] = json.loads(profile['preferences'])
        return profile
    except sqlite3.Error as e:
        logger.error(f"Database error creating profile for {user_id}: {e}")
        raise

def get_or_create_user_profile(user_id: int, admin_ids: set) -> dict:
    """A wrapper that gets a profile or creates it if it doesn't exist."""
    profile = get_user_profile(user_id)
    if not profile:
        is_premium = user_id in admin_ids
        profile = create_user_profile(user_id, is_premium=is_premium)
    return profile

def update_user_profile(user_id: int, updates: dict):
    """Updates specific fields of a user's profile."""
    # Special handling for preferences dict
    if 'preferences' in updates:
        updates['preferences'] = json.dumps(updates['preferences'])

    fields = ", ".join([f"{key} = ?" for key in updates.keys()])
    values = list(updates.values())
    values.append(user_id)

    try:
        con = sqlite3.connect(DATABASE_FILE)
        cur = con.cursor()
        cur.execute(f"UPDATE users SET {fields} WHERE user_id = ?", tuple(values))
        con.commit()
        con.close()
        logger.info(f"Updated profile for user {user_id} with data: {updates}")
    except sqlite3.Error as e:
        logger.error(f"Database error updating profile for {user_id}: {e}")
        raise

def get_all_user_ids() -> list[int]:
    """Retrieves all user IDs from the database for broadcasting."""
    try:
        con = sqlite3.connect(DATABASE_FILE)
        cur = con.cursor()
        res = cur.execute("SELECT user_id FROM users")
        user_ids = [row[0] for row in res.fetchall()]
        con.close()
        return user_ids
    except sqlite3.Error as e:
        logger.error(f"Database error getting all user IDs: {e}")
        return []