"""
Backfill incomplete conversations from Slack DM history.

Fetches the full message history from Slack for each user in the
conversations collection, and overwrites the messages array if
Slack has more messages than MongoDB.

Usage:
    heroku run python scripts/backfill_conversations.py -a sona-social-experiment

Or locally:
    python scripts/backfill_conversations.py
"""

import os
import sys
import time
import logging

from dotenv import load_dotenv
load_dotenv()

from slack_sdk import WebClient
from pymongo import MongoClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backfill")


def get_db():
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        log.error("MONGODB_URI not set")
        sys.exit(1)
    client = MongoClient(uri)
    return client.get_default_database()


def get_slack_client():
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        log.error("SLACK_BOT_TOKEN not set")
        sys.exit(1)
    return WebClient(token=token)


def fetch_dm_history(slack: WebClient, channel_id: str, limit: int = 1000):
    """Fetch full DM history from Slack, handling pagination."""
    all_messages = []
    cursor = None

    while True:
        kwargs = {"channel": channel_id, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor

        resp = slack.conversations_history(**kwargs)
        messages = resp.get("messages", [])
        all_messages.extend(messages)

        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.5)

    all_messages.reverse()
    return all_messages


def slack_messages_to_convo(slack_messages, bot_user_id: str):
    """Convert Slack message format to our conversations format."""
    converted = []
    for msg in slack_messages:
        text = msg.get("text", "").strip()
        if not text:
            continue

        user = msg.get("user")
        bot_id = msg.get("bot_id")
        subtype = msg.get("subtype")

        if subtype in ("channel_join", "channel_leave", "bot_add", "bot_remove"):
            continue

        if user == bot_user_id or bot_id:
            converted.append({"role": "assistant", "content": text})
        elif user:
            converted.append({"role": "user", "content": text})

    return converted


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Backfill incomplete conversations from Slack DM history")
    parser.add_argument("--channel", help="Only backfill this channel ID")
    parser.add_argument("--user", help="Only backfill this user ID")
    parser.add_argument("--cornell-id", help="Only backfill the user with this Cornell ID")
    args = parser.parse_args()

    db = get_db()
    slack = get_slack_client()
    conv_col = db.conversations

    auth = slack.auth_test()
    bot_user_id = auth["user_id"]
    log.info(f"Bot user ID: {bot_user_id}")

    # Build query filter
    query = {"channel_id": {"$exists": True}}
    if args.channel:
        query["channel_id"] = args.channel
    if args.user:
        query["user_id"] = args.user
    if args.cornell_id:
        query["cornell_id"] = args.cornell_id.strip().lower()

    docs = list(conv_col.find(query))
    log.info(f"Found {len(docs)} conversation documents to process")

    updated = 0
    skipped = 0
    errors = 0

    for doc in docs:
        channel_id = doc.get("channel_id")
        user_name = doc.get("user_name", "Unknown")
        db_messages = doc.get("messages", [])

        db_user_msgs = [m for m in db_messages if m.get("role") in ("user", "assistant")]
        db_count = len(db_user_msgs)

        try:
            slack_messages = fetch_dm_history(slack, channel_id)
            converted = slack_messages_to_convo(slack_messages, bot_user_id)
            slack_count = len(converted)

            if slack_count <= db_count:
                log.info(f"  {user_name} ({channel_id}): DB={db_count}, Slack={slack_count} — OK")
                skipped += 1
                continue

            log.info(f"  {user_name} ({channel_id}): DB={db_count}, Slack={slack_count} — BACKFILLING")

            system_msgs = [m for m in db_messages if m.get("role") == "system"]
            final_messages = system_msgs + converted if system_msgs else converted

            conv_col.update_one(
                {"channel_id": channel_id},
                {"$set": {
                    "messages": final_messages,
                    "updated_at": time.time(),
                    "backfilled_at": time.time(),
                }},
            )
            updated += 1
            log.info(f"  -> Updated: {db_count} -> {slack_count} messages")

        except Exception as e:
            log.error(f"  Error processing {user_name} ({channel_id}): {e}")
            errors += 1

        time.sleep(0.5)

    log.info(f"\nDone! Updated: {updated}, Skipped: {skipped}, Errors: {errors}")


if __name__ == "__main__":
    main()
    