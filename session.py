import redis
import os
import logging
import json

logger = logging.getLogger(__name__)

# --- Redis Connection ---
try:
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        raise ValueError("REDIS_URL environment variable not set.")
    # decode_responses=True is important to get strings back from Redis, not bytes
    r = redis.from_url(redis_url, decode_responses=True)
    r.ping() # Check connection
    logger.info("Successfully connected to Redis.")
except (redis.exceptions.ConnectionError, ValueError) as e:
    logger.error(f"Could not connect to Redis: {e}")
    logger.error("Session data will not be persistent. Please set REDIS_URL and ensure Redis is running.")
    r = None

# --- Key Prefixes for Redis ---
# Using prefixes helps to organize keys in Redis
WAITING_PREMIUM_Q = "queue:premium"
WAITING_REGULAR_Q = "queue:regular"
ACTIVE_CHATS_H = "active_chats" # A hash to store {user_id: partner_id}
BEHAVIOR_TRACKER_H = "behavior_tracker" # A hash for user session data

# --- Waiting Queue Management ---

def add_to_waiting_queue(user_id: int, is_premium: bool):
    if not r: return
    queue = WAITING_PREMIUM_Q if is_premium else WAITING_REGULAR_Q
    r.rpush(queue, user_id)

def remove_from_waiting_queue(user_id: int, is_premium: bool):
    if not r: return
    queue = WAITING_PREMIUM_Q if is_premium else WAITING_REGULAR_Q
    r.lrem(queue, 1, user_id)

def get_waiting_users() -> tuple[list[str], list[str]]:
    if not r: return [], []
    premium_users = r.lrange(WAITING_PREMIUM_Q, 0, -1)
    regular_users = r.lrange(WAITING_REGULAR_Q, 0, -1)
    return premium_users, regular_users

def pop_from_waiting_queue(is_premium: bool) -> str | None:
    if not r: return None
    queue = WAITING_PREMIUM_Q if is_premium else WAITING_REGULAR_Q
    return r.lpop(queue)

def get_queue_lengths() -> tuple[int, int]:
    if not r: return 0, 0
    return r.llen(WAITING_PREMIUM_Q), r.llen(WAITING_REGULAR_Q)

# --- Active Chat Management ---

def start_chat_session(user1_id: int, user2_id: int):
    if not r: return
    r.hset(ACTIVE_CHATS_H, user1_id, user2_id)
    r.hset(ACTIVE_CHATS_H, user2_id, user1_id)

def end_chat_session(user_id: int) -> int | None:
    if not r: return None
    partner_id = r.hget(ACTIVE_CHATS_H, user_id)
    if partner_id:
        r.hdel(ACTIVE_CHATS_H, user_id, partner_id)
        return int(partner_id)
    return None

def get_partner_id(user_id: int) -> int | None:
    if not r: return None
    partner_id = r.hget(ACTIVE_CHATS_H, user_id)
    return int(partner_id) if partner_id else None

def get_active_chat_count() -> int:
    if not r: return 0
    # Each chat has two entries, so we divide by 2
    return r.hlen(ACTIVE_CHATS_H) // 2


# --- Behavior Tracker Management ---

def set_behavior_tracker(user_id: int, data: dict):
    if not r: return
    r.hset(BEHAVIOR_TRACKER_H, user_id, json.dumps(data))

def get_behavior_tracker(user_id: int) -> dict | None:
    if not r: return None
    data = r.hget(BEHAVIOR_TRACKER_H, user_id)
    return json.loads(data) if data else None

def remove_behavior_tracker(user_id: int):
    if not r: return
    r.hdel(BEHAVIOR_TRACKER_H, user_id)

# --- Active Filter Management ---

ACTIVE_FILTERS_H = "active_filters"

def set_active_filter(user_id: int, gender: str):
    """Sets the active gender filter for a user's session."""
    if not r: return
    r.hset(ACTIVE_FILTERS_H, user_id, f"gender:{gender}")
    logger.info(f"Active filter for user {user_id} set to gender:{gender}")

def get_active_filter(user_id: int) -> dict | None:
    """Gets the active filter for a user."""
    if not r: return None
    filter_str = r.hget(ACTIVE_FILTERS_H, user_id)
    if not filter_str:
        return None

    try:
        filter_type, filter_value = filter_str.split(":", 1)
        return {filter_type: filter_value}
    except ValueError:
        logger.error(f"Could not parse filter string for user {user_id}: {filter_str}")
        return None

def clear_active_filter(user_id: int):
    """Clears the active filter for a user."""
    if not r: return
    r.hdel(ACTIVE_FILTERS_H, user_id)
    logger.info(f"Active filter cleared for user {user_id}")