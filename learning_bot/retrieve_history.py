import os
import json
from dotenv import load_dotenv
from slack_sdk import WebClient

# Load environment variables
load_dotenv()
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]

web = WebClient(token=SLACK_BOT_TOKEN)


def find_user_by_name(name):
    """Find a Slack user by their display name or real name"""
    try:
        response = web.users_list()
        for user in response["members"]:
            # Check display name, real name, or username
            display_name = user.get("profile", {}).get("display_name", "").lower()
            real_name = user.get("profile", {}).get("real_name", "").lower()
            username = user.get("name", "").lower()

            if name.lower() in [display_name, real_name, username]:
                return user
        return None
    except Exception as e:
        print(f"Error finding user: {e}")
        return None


def get_dm_channel(user_id):
    """Get or create a DM channel with a user"""
    try:
        response = web.conversations_open(users=[user_id])
        return response["channel"]["id"]
    except Exception as e:
        print(f"Error getting DM channel: {e}")
        return None


def get_conversation_history(channel_id, limit=100):
    """Retrieve conversation history from a channel"""
    try:
        response = web.conversations_history(channel=channel_id, limit=limit)
        messages = response["messages"]

        # Sort messages by timestamp (oldest first)
        messages.sort(key=lambda x: float(x["ts"]))

        return messages
    except Exception as e:
        print(f"Error retrieving conversation history: {e}")
        return []


def format_message(msg, bot_user_id):
    """Format a message for display"""
    user_id = msg.get("user", "unknown")
    text = msg.get("text", "")
    timestamp = msg.get("ts", "")

    # Determine if it's from the bot
    role = "Bot" if user_id == bot_user_id else "User"

    return {"role": role, "user_id": user_id, "timestamp": timestamp, "text": text}


if __name__ == "__main__":
    # Get bot user ID
    auth = web.auth_test()
    bot_user_id = auth["user_id"]

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

    # Get DM channel
    print(f"\nRetrieving DM channel...")
    dm_channel_id = get_dm_channel(user_id)

    if not dm_channel_id:
        print("Could not open DM channel with user.")
        exit(1)

    print(f"DM Channel ID: {dm_channel_id}")

    # Get conversation history
    print(f"\nRetrieving conversation history...")
    messages = get_conversation_history(dm_channel_id)

    if not messages:
        print("No messages found in the conversation.")
        exit(0)

    print(f"\nFound {len(messages)} messages:")
    print("=" * 80)

    # Format and display messages
    conversation = []
    for msg in messages:
        formatted = format_message(msg, bot_user_id)
        conversation.append(formatted)

        print(f"\n[{formatted['role']}] at {formatted['timestamp']}")
        print(f"{formatted['text']}")
        print("-" * 80)

    # Save to JSON file
    output_file = "jieun_conversation_history.json"
    with open(output_file, "w") as f:
        json.dump(conversation, f, indent=2)

    print(f"\n\nConversation history saved to: {output_file}")
