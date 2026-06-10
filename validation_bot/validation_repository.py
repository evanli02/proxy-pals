import logging
from commons.db import (
    get_conversations_collection,
    get_validation_collection
)
from validation_bot.models import ValidationDocument

log = logging.getLogger("validation_repository")

class ValidationRepository:
    """Handles all database operations related to validation."""
    
    def __init__(self):
        # Collections are fetched lazily or initialized here
        pass

    def get_messages_by_user(self, user_id: str) -> list:
        """
        Retrieve messages for a given user_id from the conversations collection.
        Finds the most recently updated document for the user and extracts its 'messages'.
        """
        col = get_conversations_collection()
        if col is None:
            log.warning("MongoDB conversations collection not configured")
            return []

        try:
            cursor = col.find({"user_id": user_id}).sort("updated_at", -1).limit(1)
            doc = next(cursor, None)
            if doc and "messages" in doc:
                return doc.get("messages", [])
            return []
        except Exception as e:
            log.error(f"Error fetching messages for user {user_id}: {e}")
            return []

    def upsert_validation(self, document: ValidationDocument):
        """
        Saves the validation document to the DB using its internal update dictionary logic.
        """
        validation_col = get_validation_collection()
        if validation_col is None:
            log.warning("MongoDB validation collection not configured. Cannot save validation.")
            return
        
        try:
            validation_col.update_one(
                {"user_id": document.user_id},
                document.to_mongo_update_dict(),
                upsert=True
            )
            log.info(f"Successfully upserted validation for user {document.user_id}")
        except Exception as e:
            log.error(f"Failed to upsert validation for user {document.user_id}: {e}")

    def upsert_masked_validation(self, document: ValidationDocument):
        """
        Saves the masked validation document fields to the DB.
        """
        validation_col = get_validation_collection()
        if validation_col is None:
            log.warning("MongoDB validation collection not configured. Cannot save masked validation.")
            return
        
        try:
            validation_col.update_one(
                {"user_id": document.user_id},
                document.to_masked_mongo_update_dict(),
                upsert=True
            )
            log.info(f"Successfully upserted masked validation for user {document.user_id}")
        except Exception as e:
            log.error(f"Failed to upsert masked validation for user {document.user_id}: {e}")
