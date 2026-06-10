import datetime
import logging
from pymongo.errors import PyMongoError
from commons.db import (
    get_proxy_collection,
    get_archived_proxy_collection,
    get_conversations_collection
)

log = logging.getLogger("history")


def archive_and_clear_history(channel_id: str, clear_proxy: bool = False, clear_learning: bool = False):
    """
    Archives and clears history for the proxy bot and/or learning bot.
    Throws PyMongoError if the archive operation fails, halting the process.
    """
    if clear_proxy:
        proxy_col = get_proxy_collection()
        archived_col = get_archived_proxy_collection()

        if proxy_col is not None and archived_col is not None:
            # 1. Fetch the existing proxy document
            doc = proxy_col.find_one({"channel_id": channel_id})
            if doc:
                # 2. Add archive metadata
                doc["archived_at"] = datetime.datetime.utcnow()
                # Remove _id to avoid collision if we re-archive
                doc.pop("_id", None)

                # 3. Insert to archived collection (Atomicity check)
                try:
                    archived_col.insert_one(doc)
                    log.info(f"Archived proxy conversation for channel {channel_id}")
                except PyMongoError as e:
                    log.error(f"Failed to archive proxy conversation: {e}")
                    raise  # Propagate the error to abort deletion and caller flow
            else:
                log.info(f"No proxy conversation found to archive for channel {channel_id}")

            # 4. Delete and clear in-memory state
            import proxy_bot.proxy_bot_service as pbs
            pbs.clear_proxy_history(channel_id)
            log.info(f"Cleared proxy history for channel {channel_id}")
