import os
import json
from dotenv import load_dotenv
from slack_sdk import WebClient

# Load environment variables
load_dotenv()
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
CONVERSATIONS_FILE = "conversations.json"

web = WebClient(token=SLACK_BOT_TOKEN)


def find_user_by_name(name):
    """Find a Slack user by their display name or real name"""
    try:
        response = web.users_list()
        for user in response["members"]:
            display_name = user.get("profile", {}).get("display_name", "").lower()
            real_name = user.get("profile", {}).get("real_name", "").lower()
            username = user.get("name", "").lower()

            if name.lower() in [display_name, real_name, username]:
                return user
        return None
    except Exception as e:
        print(f"Error finding user: {e}")
        return None


def find_dm_channel_id(user_id):
    """Find the DM channel ID for a specific user by looking at conversations list"""
    try:
        # List all conversations the bot is part of
        response = web.conversations_list(types="im")
        for channel in response["channels"]:
            # For DM channels, check if the user matches
            if channel.get("user") == user_id:
                return channel["id"]
        return None
    except Exception as e:
        print(f"Error finding DM channel: {e}")
        return None


def load_conversations():
    """Load conversation history from JSON file"""
    try:
        if os.path.exists(CONVERSATIONS_FILE):
            with open(CONVERSATIONS_FILE, "r") as f:
                return json.load(f)
        else:
            print(f"No conversations file found at {CONVERSATIONS_FILE}")
            return {}
    except Exception as e:
        print(f"Error loading conversations: {e}")
        return {}


def format_conversation(conversation):
    """Format conversation for display"""
    formatted_lines = []
    for msg in conversation:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")

        if role == "system":
            formatted_lines.append(f"\n{'='*80}")
            formatted_lines.append(f"[SYSTEM PROMPT]")
            formatted_lines.append(f"{'='*80}")
            formatted_lines.append(
                content[:200] + "..." if len(content) > 200 else content
            )
        elif role == "user":
            formatted_lines.append(f"\n{'='*80}")
            formatted_lines.append(f"[USER]")
            formatted_lines.append(f"{'='*80}")
            formatted_lines.append(content)
        elif role == "assistant":
            formatted_lines.append(f"\n{'='*80}")
            formatted_lines.append(f"[ASSISTANT]")
            formatted_lines.append(f"{'='*80}")
            formatted_lines.append(content)

    return "\n".join(formatted_lines)


if __name__ == "__main__":
    # Find jieun user
    print("Searching for user 'jieun'...")
    user = find_user_by_name("jieun")

    if not user:
        print("User 'jieun' not found in the workspace.")
        exit(1)

    user_id = user["id"]
    display_name = user.get("profile", {}).get("display_name", user.get("name"))
    real_name = user.get("profile", {}).get("real_name", "")

    print(f"\nFound user:")
    print(f"  Display Name: {display_name}")
    print(f"  Real Name: {real_name}")
    print(f"  User ID: {user_id}")

    # Find DM channel ID
    print(f"\nSearching for DM channel...")
    dm_channel_id = find_dm_channel_id(user_id)

    if not dm_channel_id:
        print("Could not find DM channel with user.")
        print("\nNote: The bot may not have a DM conversation with this user yet.")
        exit(1)

    print(f"DM Channel ID: {dm_channel_id}")

    # Load conversations from file
    print(f"\nLoading conversations from {CONVERSATIONS_FILE}...")
    conversations = load_conversations()

    if not conversations:
        print("No conversations found in the file.")
        exit(0)

    # Find conversation for this channel
    if dm_channel_id not in conversations:
        print(f"\nNo conversation history found for channel {dm_channel_id}")
        print(f"\nAvailable channels in conversations.json:")
        for channel_id in conversations.keys():
            print(f"  - {channel_id} ({len(conversations[channel_id])} messages)")
        exit(0)

    conversation = conversations[dm_channel_id]

    print(f"\n{'='*80}")
    print(f"CONVERSATION HISTORY FOR {real_name or display_name}")
    print(f"Channel ID: {dm_channel_id}")
    print(f"Total messages: {len(conversation)}")
    print(f"{'='*80}")

    # Display formatted conversation
    print(format_conversation(conversation))

    # Save to a separate file
    output_file = f"conversation_{user_id}_{dm_channel_id}.json"
    with open(output_file, "w") as f:
        json.dump(conversation, f, indent=2)

    print(f"\n\n{'='*80}")
    print(f"Conversation also saved to: {output_file}")
    print(f"{'='*80}")
