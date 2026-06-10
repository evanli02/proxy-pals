import os
import time
import logging
from dotenv import load_dotenv
from openai import OpenAI
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

# ---- Logging setup --------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("streaming_slackbot")

# Load environment variables
load_dotenv()

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
ASSISTANT_ID = os.environ["ASSISTANT_ID"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]

# Initialize clients
oai = OpenAI(api_key=OPENAI_API_KEY)
web = WebClient(token=SLACK_BOT_TOKEN)
socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=web)

auth = web.auth_test()
BOT_USER_ID = auth["user_id"]
log.info(f"Connected to Slack as bot user {BOT_USER_ID}")
log.info(f"Using OpenAI Assistant {ASSISTANT_ID}")

# State
seen_events = set()
channel_threads = {}


def handle_message_with_streaming(event):
    """Handle Slack message with streaming Assistants API and message updates"""
    if event.get("subtype") or not event.get("text"):
        return

    # Ignore messages from the bot itself
    if event.get("user") == BOT_USER_ID:
        return

    ev_id = event.get("client_msg_id") or f"{event['ts']}:{event['channel']}"
    if ev_id in seen_events:
        return
    seen_events.add(ev_id)

    channel = event["channel"]
    text = event["text"]

    log.info(f"Received message: {text}")

    try:
        # Step 1: Post initial "thinking" message
        thinking_response = web.chat_postMessage(
            channel=channel, text="🤔 Processing your request..."
        )
        thinking_ts = thinking_response["ts"]

        # Get or create OpenAI thread for this channel
        if channel not in channel_threads:
            thread = oai.beta.threads.create()
            channel_threads[channel] = thread.id
            log.info(f"Created new OpenAI thread for channel")

        thread_id = channel_threads[channel]

        # Add user message to thread
        oai.beta.threads.messages.create(thread_id=thread_id, role="user", content=text)

        # Stream the assistant's response
        stream = oai.beta.threads.runs.create(
            thread_id=thread_id, assistant_id=ASSISTANT_ID, stream=True
        )

        # Collect response as it streams
        response_parts = []
        last_update_time = time.time()
        update_interval = 1.0  # Update Slack message every 1 second

        for event in stream:
            if event.event == "thread.message.delta":
                # Extract the text content from the delta
                if hasattr(event.data, "delta") and hasattr(
                    event.data.delta, "content"
                ):
                    for content in event.data.delta.content:
                        if content.type == "text" and hasattr(content, "text"):
                            text_value = content.text.value
                            response_parts.append(text_value)

                            # Update Slack message periodically during streaming
                            current_time = time.time()
                            if current_time - last_update_time >= update_interval:
                                partial_response = "".join(response_parts)
                                if partial_response.strip():
                                    web.chat_update(
                                        channel=channel,
                                        ts=thinking_ts,
                                        text=f"✍️ {partial_response}...",
                                    )
                                    last_update_time = current_time

            elif event.event == "thread.run.completed":
                log.info("Assistant run completed")
                break

            elif event.event == "thread.run.failed":
                log.error(f"Assistant run failed: {event.data}")
                web.chat_update(
                    channel=channel,
                    ts=thinking_ts,
                    text="❌ Sorry, I encountered an error processing your request.",
                )
                return

        # Step 2: Update with final complete response
        final_response = "".join(response_parts)
        if final_response.strip():
            web.chat_update(channel=channel, ts=thinking_ts, text=final_response)
            log.info(f"Updated message with final response: {final_response[:80]}...")
        else:
            web.chat_update(
                channel=channel,
                ts=thinking_ts,
                text="🤷 I couldn't generate a response to that.",
            )

    except Exception as e:
        log.error(f"Error handling message: {e}")
        # Try to update the thinking message with an error
        try:
            web.chat_update(
                channel=channel,
                ts=thinking_ts,
                text="❌ Sorry, I encountered an error processing your request.",
            )
        except:
            # If update fails, post a new message
            web.chat_postMessage(
                channel=channel,
                text="❌ Sorry, I encountered an error processing your request.",
            )


def process(client: SocketModeClient, req: SocketModeRequest):
    """Process incoming Slack events"""
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    if req.type == "events_api":
        ev = req.payload.get("event", {})
        if ev.get("type") == "message":
            handle_message_with_streaming(ev)


if __name__ == "__main__":
    socket_client.socket_mode_request_listeners.append(process)
    log.info("🚀 Streaming Slack bot with message updates started!")
    socket_client.connect()

    try:
        # Simple blocking loop
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Bot shutting down...")
