import os
import logging
from typing import Optional

from dotenv import load_dotenv
from pymongo import MongoClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("db")

load_dotenv()

_MONGO_CLIENT: Optional[MongoClient] = None


def get_mongo_client() -> Optional[MongoClient]:
    global _MONGO_CLIENT
    if _MONGO_CLIENT is not None:
        return _MONGO_CLIENT

    mongodb_uri = os.environ.get("MONGODB_URI")
    if not mongodb_uri:
        log.error("MONGODB_URI not set in environment variables")
        return None

    try:
        _MONGO_CLIENT = MongoClient(mongodb_uri)
        _ = _MONGO_CLIENT.get_database()
        log.info("MongoDB client initialized (singleton)")
        return _MONGO_CLIENT
    except Exception as e:
        log.error(f"Failed to create MongoDB client: {e}")
        _MONGO_CLIENT = None
        return None


def get_db():
    client = get_mongo_client()
    if client is None:
        return None
    try:
        return client.get_database()
    except Exception as e:
        log.error(f"Failed to get database from client: {e}")
        return None


def get_conversations_collection():
    database = get_db()
    if database is None:
        return None
    try:
        return database.conversations
    except Exception as e:
        log.error(f"Failed to access conversations collection: {e}")
        return None


def get_qa_pairs_collection():
    db = get_db()
    return db.qa_pairs


def get_proxy_collection():
    """Get the proxy bot conversations collection (slackbot.proxy)."""
    database = get_db()
    if database is None:
        return None
    try:
        return database.proxy
    except Exception as e:
        log.error(f"Failed to access proxy collection: {e}")
        return None


def get_archived_proxy_collection():
    """Get the archived proxy bot conversations collection."""
    database = get_db()
    if database is None:
        return None
    try:
        return database.archived_proxy_conversation
    except Exception as e:
        log.error(f"Failed to access archived proxy collection: {e}")
        return None


def get_partner_maps_collection():
    """Collection storing the global partner map (single document)."""
    database = get_db()
    if database is None:
        return None
    try:
        return database.partner_maps
    except Exception as e:
        log.error(f"Failed to access partner_maps collection: {e}")
        return None

def get_validation_collection():
    database = get_db()
    if database is None:
        return None
    try:
        return database.validation
    except Exception as e:
        log.error(f"Failed to access validation collection: {e}")
        return None


def get_unanswered_questions_collection():
    """Collection storing questions the proxy bot couldn't answer."""
    database = get_db()
    if database is None:
        return None
    try:
        return database.unanswered_questions
    except Exception as e:
        log.error(f"Failed to access unanswered_questions collection: {e}")
        return None
