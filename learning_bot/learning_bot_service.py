import datetime
import json
import logging
import os
import threading
import time
from typing import Dict, List, Optional, Tuple, Any, cast

from flask import Flask, request, jsonify

from commons.clients import make_slack_client, make_openai_client, get_signing_secret
from commons.db import get_mongo_client, get_db, get_conversations_collection, get_unanswered_questions_collection, \
    get_partner_maps_collection
from commons.onboarding import onboarding_message, farewell_message
from commons.slack import verify_slack_signature
from learning_bot.learning_bot_prompt import get_learning_bot_prompt
from learning_bot.question_selector import (
    get_next_main_question, get_follow_up_questions
)

# ---- Logging setup --------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("slackbot")

# ---- Flask app ------------------------------------------------------------
app = Flask(__name__)

# ---- Clients --------------------------------------------------------------
web, BOT_USER_ID = make_slack_client("SLACK_BOT_TOKEN")
oai = make_openai_client()
SLACK_SIGNING_SECRET = get_signing_secret("SLACK_SIGNING_SECRET")
MODEL = "gpt-5-mini"

# Debug: dump full in-memory queues on every operation (very verbose)
DEBUG_QUEUE_DUMP = os.environ.get("DEBUG_QUEUE_DUMP", "1").lower() in ("1", "true", "yes", "on")

# ---- MongoDB Setup (Singleton) -------------------------------------------
mongo_client = get_mongo_client()
db = get_db() if mongo_client is not None else None
col = db.conversations_collection if db is not None else None

conversations_collection = get_conversations_collection() if db is not None else None
if conversations_collection is not None:
    log.info("Connected to MongoDB (singleton)")
else:
    log.warning(
        "MongoDB not configured or connection failed; conversations may not persist"
    )


# ---- State for conversation context --------------------------------------
# Thread-safe per-channel conversation buffer and background persistence

class ChannelConversation:
    """A thread-safe in-memory buffer for a channel's conversation and state.

    - messages: full conversation history (system + user/assistant)
    - save_cursor: index in messages up to which DB is persisted (exclusive)
    - asked_ids, follow_up_ids, previous_question: state kept in-memory
    - user_id, user_name: metadata
    """

    def __init__(self, channel_id: str,
                 messages: Optional[List[dict]] = None,
                 state: Optional[dict] = None,
                 user_id: Optional[str] = None,
                 user_name: Optional[str] = None):
        self.channel_id = channel_id
        self._lock = threading.RLock()
        self.messages = messages[:] if messages else []
        self.save_cursor = len(self.messages)
        state = state or {}
        self.asked_ids = set(state.get("asked_ids", []))
        self.follow_up_ids = set(state.get("follow_up_ids", []))
        self.previous_question = state.get("previous_question", "")
        self.previous_question_id = state.get("previous_question_id", "")
        self.user_id = user_id
        self.user_name = user_name
        self.partner_map_initialized = bool(state.get("partner_map_initialized", False))
        # Flag to append onboarding message to the first assistant reply
        self.append_onboarding = bool(state.get("append_onboarding", False))
        self._state_dirty = False
        self.last_updated_at = time.time()
        self._state_version = 0  # increments on state changes

    def lock(self):
        return self._lock

    def _dump_queue(self, action: str, reason: Optional[str] = None, messages: Optional[List[dict]] = None):
        """Verbose dump of the queue for debugging missing messages.
        Call only while holding the lock for consistency.
        """
        if not DEBUG_QUEUE_DUMP:
            return
        msgs = messages if messages is not None else self.messages
        header = f"[QUEUE DUMP] channel={self.channel_id} action={action} size={len(msgs)}"
        if reason:
            header += f" reason='{reason}'"
        # Include state snapshot too
        header += (
            f" | state: prev='{self.previous_question}' prev_id='{self.previous_question_id}' "
            f"asked_ids={sorted(list(self.asked_ids))} follow_up_ids={sorted(list(self.follow_up_ids))} append_onboarding={self.append_onboarding}"
        )
        log.info(header)

    def ensure_system_prompt(self, prompt: str):
        """Set or update the system prompt as first message.
        Should be called under lock.
        """
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = prompt
        else:
            self.messages.insert(0, {"role": "system", "content": prompt})
        self.last_updated_at = time.time()
        self._dump_queue("ensure_system_prompt", "set/update system prompt")

    def append_message(self, role: str, content: str):
        """Append a new message to the buffer. Should be called under lock."""
        self.messages.append({"role": role, "content": content})
        self.last_updated_at = time.time()
        self._dump_queue("append_message", f"role={role}")

    def set_user_meta(self, user_id: Optional[str], user_name: Optional[str]):
        if user_id:
            self.user_id = user_id
        if user_name:
            self.user_name = user_name

    def mark_state(self, *, previous_question: Optional[str] = None,
                   previous_question_id: Optional[str] = None,
                   add_asked_id: Optional[str] = None,
                   add_follow_up_id: Optional[str] = None,
                   clear_follow_ups: bool = False):
        """Update in-memory state; log on each change."""
        if previous_question is not None:
            self.previous_question = previous_question
        if previous_question_id is not None:
            self.previous_question_id = previous_question_id
        if add_asked_id:
            self.asked_ids.add(add_asked_id)
        if add_follow_up_id:
            self.follow_up_ids.add(add_follow_up_id)
        if clear_follow_ups:
            self.follow_up_ids.clear()
        self._state_dirty = True
        self._state_version += 1
        log.info(
            f"[STATE UPDATE] channel={self.channel_id} prev='{self.previous_question}' "
            f"prev_id='{self.previous_question_id}' asked_ids={sorted(list(self.asked_ids))} follow_up_ids={sorted(list(self.follow_up_ids))}"
        )

    def get_messages_snapshot(self) -> List[Dict[str, str]]:
        """Return a shallow copy of messages for safe, lock-free reads."""
        return self.messages[:]

    def needs_persist(self) -> bool:
        return self._state_dirty or (self.save_cursor < len(self.messages))

    def snapshot_for_persist(self) -> Tuple[List[dict], dict, Optional[str], Optional[str], int, int]:
        """Return a snapshot of messages/state for persistence, including the
        message count and state version captured at snapshot time. Should be under lock.
        """
        state = {
            "previous_question": self.previous_question,
            "previous_question_id": self.previous_question_id,
            "asked_ids": list(self.asked_ids),
            "follow_up_ids": list(self.follow_up_ids),
            "partner_map_initialized": self.partner_map_initialized,
        }
        saved_len = len(self.messages)
        state_ver = self._state_version
        self._dump_queue("snapshot_for_persist", f"len={saved_len}")
        return (self.messages[:], state, self.user_id, self.user_name, saved_len, state_ver)

    def mark_persisted(self, saved_len: int, saved_state_version: int):
        # Only mark messages up to saved_len as persisted; newer appends remain pending
        if saved_len > self.save_cursor:
            self.save_cursor = saved_len
        # Clear state dirty flag only if no newer state changes occurred
        if self._state_version == saved_state_version:
            self._state_dirty = False
        self._dump_queue("mark_persisted", f"saved_len={saved_len} state_ver={saved_state_version}")


# Global maps and locks
channel_buffers: Dict[str, ChannelConversation] = {}
_channel_map_lock = threading.RLock()

# Per-channel processing locks to prevent race conditions
channel_processing_locks: Dict[str, threading.Lock] = {}
_processing_locks_lock = threading.Lock()

# Track pending incomplete responses with timers (currently not used for delayed responses)
pending_responses = ({})  # {channel_id: {"response": str, "timer": Timer, "full_reply": str}}


def get_or_create_buffer(channel_id: str) -> ChannelConversation:
    """Get a channel buffer, loading from DB only once on first access."""
    with _channel_map_lock:
        buf = channel_buffers.get(channel_id)
        if buf:
            return buf

    # Load from DB outside map lock to avoid holding it while doing I/O
    messages, state = load_conversation(channel_id)
    if not messages:
        # Initialize with default system prompt; will be updated in handler
        messages = [{"role": "system", "content": get_learning_bot_prompt()}]

    buf = ChannelConversation(channel_id, messages=messages, state=state)
    with _channel_map_lock:
        # Double-check to avoid race where another thread created the buffer
        existing = channel_buffers.get(channel_id)
        if existing:
            return existing
        channel_buffers[channel_id] = buf
        # Log initial queue contents after creation
        with buf.lock():
            buf._dump_queue("create_buffer", "loaded from DB or initialized")
        return buf


def get_channel_processing_lock(channel_id: str) -> threading.Lock:
    """Get or create a processing lock for a specific channel to prevent race conditions.
    
    This ensures only one message per channel is processed at a time, preventing
    the race condition where two threads might select the same question before
    either marks it as asked.
    """
    with _processing_locks_lock:
        if channel_id not in channel_processing_locks:
            channel_processing_locks[channel_id] = threading.Lock()
        return channel_processing_locks[channel_id]


def _background_persist_loop(interval_seconds: float = 1.0):
    """Periodically persist any buffers with unsaved messages/state to MongoDB."""
    while True:
        try:
            with _channel_map_lock:
                buffers = list(channel_buffers.values())
            for buf in buffers:
                with buf.lock():
                    if not buf.needs_persist():
                        continue
                    messages_snapshot, state_snapshot, user_id, user_name, saved_len, state_ver = buf.snapshot_for_persist()
                # Persist outside the per-buffer lock to avoid blocking producers
                try:
                    save_conversation(
                        buf.channel_id,
                        messages_snapshot,
                        set(state_snapshot.get("asked_ids", [])),
                        user_id=user_id,
                        user_name=user_name,
                        follow_up_ids=set(state_snapshot.get("follow_up_ids", [])),
                        previous_question=state_snapshot.get("previous_question", ""),
                        previous_question_id=state_snapshot.get("previous_question_id", ""),
                        partner_map_initialized=state_snapshot.get("partner_map_initialized", False),
                    )
                    with buf.lock():
                        buf.mark_persisted(saved_len, state_ver)
                except Exception as e:
                    log.error(f"Background persist failed for channel {buf.channel_id}: {e}")
        except Exception as e:
            log.error(f"Background persist loop error: {e}")
        time.sleep(interval_seconds)


# Start background persistence thread once at import
_persist_thread = threading.Thread(target=_background_persist_loop, args=(1.0,), daemon=True)
_persist_thread.start()


def save_conversation(channel_id, messages, asked_question,
                      user_id=None, user_name=None,
                      follow_up_ids=None,
                      previous_question=None,
                      previous_question_id=None,
                      partner_map_initialized=False):
    """Save conversation and state to MongoDB"""
    if follow_up_ids is None:
        follow_up_ids = []
    if conversations_collection is None:
        log.warning("MongoDB not configured, skipping save")
        return
    if previous_question is None:
        previous_question = ""
    if previous_question_id is None:
        previous_question_id = ""

    try:
        update_data = {
            "channel_id": channel_id,
            "user_id": user_id,
            "user_name": user_name,
            "messages": messages,
            "updated_at": time.time(),
            "state": {
                "previous_question": previous_question,
                "previous_question_id": previous_question_id,
                "asked_ids": list(asked_question),
                "follow_up_ids": list(follow_up_ids),
                "partner_map_initialized": partner_map_initialized,
            }
        }
        # Save state (convert sets to lists for JSON serialization)

        conversations_collection.update_one(
            {"channel_id": channel_id},
            {"$set": update_data},
            upsert=True,
        )
    except Exception as e:
        log.error(f"Error saving conversation to MongoDB: {e}")


def load_conversation(channel_id):
    """Load conversation from MongoDB"""
    if conversations_collection is None:
        log.warning("MongoDB not configured, returning empty conversation")
        return None, None

    try:
        doc = conversations_collection.find_one({"channel_id": channel_id})
        if doc is None:
            # New conversation: do NOT post onboarding immediately.
            # Signal to append onboarding to the first assistant response instead.
            return None, {"append_onboarding": True}
        if doc and "messages" in doc:
            log.info(f"Loaded conversation for channel {channel_id} from MongoDB")
            messages = doc["messages"]

            # Load state if it exists
            state = None
            if "state" in doc:
                state_data = doc["state"]
                state = {
                    "asked_ids": set(state_data.get("asked_ids", [])),
                    "follow_up_ids": set(state_data.get("follow_up_ids", [])),
                    "previous_question": state_data.get("previous_question", ""),
                    "previous_question_id": state_data.get("previous_question_id", ""),
                    "partner_map_initialized": state_data.get("partner_map_initialized", False),
                }
                log.info(
                    f"[STATE LOADED] Loaded state for channel {channel_id}: "
                    f"asked_ids={sorted(list(state['asked_ids']))}, prev_id='{state['previous_question_id']}'"
                )

            return messages, state
        return None, None
    except Exception as e:
        log.error(f"Error loading conversation from MongoDB: {e}")
        return None, None


def send_delayed_response(
        channel, response_text, full_reply, user_id, user_name, asked_ids,
        follow_up_ids,
        previous_question,
        previous_question_id
):
    """Send a response after 10 seconds if no new message arrived"""
    log.info(f"10 seconds elapsed, sending delayed response to channel {channel}")

    try:
        # Validate response text
        if not response_text or response_text.strip() == "":
            log.warning(f"Response text is empty, not sending delayed response")
            # Clean up pending response
            if channel in pending_responses:
                del pending_responses[channel]
            return

        # Post the response directly (no thinking message for delayed responses)
        web.chat_postMessage(channel=channel, text=response_text)
        log.info(f"Posted delayed reply: {response_text[:80]}...")

        # Add assistant response to conversation history using buffer
        try:
            parsed_response = json.loads(full_reply)
            response_text = parsed_response.get("response", response_text)
        except Exception:
            pass

        buf = get_or_create_buffer(channel)
        with buf.lock():
            # Align state passed in
            buf.set_user_meta(user_id, user_name)
            buf.asked_ids = set(asked_ids or [])
            buf.follow_up_ids = set(follow_up_ids or [])
            buf.previous_question = previous_question or ""
            buf.previous_question_id = previous_question_id or ""
            buf._state_dirty = True
            buf.append_message("assistant", response_text)

        # Clean up pending response
        if channel in pending_responses:
            del pending_responses[channel]

    except Exception as e:
        log.error(f"Error sending delayed response: {e}")


def handle_message(event):
    """Handle a Slack message: append to in-memory buffer, call OpenAI, append response, then post to Slack.
    
    Uses per-channel processing lock to prevent race conditions where multiple threads
    might process messages for the same channel simultaneously and select the same question.
    """
    if event.get("subtype") or not event.get("text"):
        return
    if event.get("user") == BOT_USER_ID:
        return
    channel = event["channel"]

    # CRITICAL: Acquire per-channel processing lock to prevent race conditions
    # This ensures messages for the same channel are processed sequentially
    processing_lock = get_channel_processing_lock(channel)
    with processing_lock:
        _handle_message_internal(event, channel)


def _handle_message_internal(event, channel):
    """Internal message handler - must be called with channel processing lock held."""
    text = event["text"]

    log.info(f"Received message: {text}")

    if text.lower().strip() == "clear":
        clear_history(channel)
        return

    # user can type "review" to start answering questions their proxy couldn't handle
    if text.lower().strip() == "review":
        user_id = event.get("user")
        if not user_id:
            return
        uq_col = get_unanswered_questions_collection()
        if uq_col is None:
            web.chat_postMessage(channel=channel, text="Couldn't connect to the database right now, try again later.")
            return
        gaps = list(uq_col.find({
            "partner_id": user_id,
            "answered": False,
            "skipped": {"$ne": True},
        }))
        if not gaps:
            web.chat_postMessage(channel=channel, text="No unanswered questions right now! Your proxy is all caught up.")
            return
        questions = [{"question": g["question"], "category": g.get("category", "unknown")} for g in gaps]
        _start_gap_session(channel, user_id, questions)
        return

    # check if this channel has an active gap review session — if so,
    # route the message there instead of the normal question flow
    if _handle_gap_response(channel, text):
        return

    user_id = event.get("user")

    # check if we're waiting for this user's cornell ID
    if _handle_cornell_id_response(channel, user_id, text):
        return

    # Cancel any pending timer for this channel (user is continuing their message)
    if channel in pending_responses:
        try:
            timer = pending_responses[channel].get("timer")
            if timer:
                timer.cancel()
        except Exception:
            pass
        del pending_responses[channel]

    # Get user info from Slack
    user_name = None
    try:
        user_info = web.users_info(user=user_id)
        if user_info and user_info.get("ok"):
            user_obj = user_info.get("user") or {}
            user_profile = user_obj.get("profile") or {}
            user_name = (
                    user_profile.get("display_name")
                    or user_profile.get("real_name")
                    or user_obj.get("name")
            )
            log.info(f"Message from user: {user_name} (ID: {user_id})")
    except Exception as e:
        log.warning(f"Could not fetch user info: {e}")

    try:
        # Get or create in-memory buffer (load from DB only once)
        buf = get_or_create_buffer(channel)

        # Check if this user needs to provide their Cornell ID before starting
        if user_id and not _user_has_cornell_id(channel, user_id):
            # first message from this user — ask for cornell ID
            _prompt_for_cornell_id(channel, user_id)
            return

        # Update user metadata on buffer
        with buf.lock():
            log.info("Current queue size: " + str(len(buf.messages)))
            buf.set_user_meta(user_id, user_name)

            # Always record the incoming user message, even if there is no next question
            buf.append_message("user", text)

            if user_id and not buf.partner_map_initialized:
                # auto-create partner map so the user can immediately chat with their own proxy.
                try:
                    partner_col = get_partner_maps_collection()
                    partner_col.update_one(
                        {"_id": "partner_map"},
                        {"$set": {
                            f"map.{user_id}": {
                                "partner_id": user_id,
                                "mode": "mimic"
                            },
                            "updated_at": datetime.datetime.utcnow()
                        }},
                        upsert=True,
                    )
                    buf.partner_map_initialized = True
                    buf._state_dirty = True
                except Exception as e:
                    log.error(f"Failed updating partner map for {user_id}: {e}")

            asked_ids = set(buf.asked_ids)
            follow_up_ids = set(buf.follow_up_ids)
            previous_question = buf.previous_question or ""
            previous_question_id = buf.previous_question_id or ""
            follow_up_questions = (
                get_follow_up_questions(previous_question_id, follow_up_ids)
                if previous_question_id else []
            )
            next_question = get_next_main_question(asked_ids)
            log.info(f"Next question: {next_question}")
            if next_question is None:
                # Interview is complete — send farewell.
                web.chat_postMessage(channel=channel, text=farewell_message())
                return
            # Update system prompt for this turn (kept at index 0)
            # If next_question is None but follow-ups remain, pass empty string
            # so the LLM only chooses from follow-ups.
            next_main_question_text = next_question["main_question"] if next_question else ""
            updated_system_prompt = get_learning_bot_prompt(
                next_main_question_text,
                follow_up_questions,
                previous_question,
                text,
            )
            buf.ensure_system_prompt(updated_system_prompt)

            # Take a snapshot for the OpenAI call without holding the lock
            messages_to_send = buf.get_messages_snapshot()
        # Stream response from OpenAI with full conversation context
        messages_payload = cast(List[Dict[str, Any]], messages_to_send)
        response = oai.chat.completions.create(
            model=MODEL,
            messages=messages_payload,  # type: ignore[arg-type]
            response_format={"type": "json_object"}
        )
        raw_content = response.choices[0].message.content

        try:
            # Parse the JSON response
            parsed_response = json.loads(raw_content or "{}")
            response_text = parsed_response.get("response", "")
            need_followup = parsed_response.get("need_followup", False)
            follow_up_id = parsed_response.get("follow_up_id", "")

            # Compute final response text, appending onboarding if needed
            final_text = response_text
            with buf.lock():
                if buf.append_onboarding:
                    final_text = f"{onboarding_message()}\n\n{response_text}" if response_text else onboarding_message()
                    buf.append_onboarding = False

                if need_followup and follow_up_id:
                    buf.mark_state(add_follow_up_id=follow_up_id)
                elif next_question is not None:
                    buf.mark_state(
                        previous_question=next_question["main_question"],
                        previous_question_id=next_question["id"],
                        add_asked_id=next_question["id"],
                        clear_follow_ups=True,
                    )
                # Verify response
                if not final_text or final_text.strip() == "":
                    log.warning("Response is marked complete but text is empty, skipping send")
                    return
                buf.append_message("assistant", final_text)

            # Verify we have valid response text before sending
            if not final_text or final_text.strip() == "":
                log.warning(
                    "Response is marked complete but text is empty, skipping"
                )
                return

            web.chat_postMessage(channel=channel, text=final_text)
            log.info(f"Posted reply: {final_text[:80]}...")

        except json.JSONDecodeError as e:
            log.warning(f"Failed to parse JSON response: {e}, using raw text")
            # Prepare final text with onboarding if needed
            fallback_text = raw_content or ""
            with buf.lock():
                if buf.append_onboarding:
                    fallback_text = f"{onboarding_message()}\n\n{fallback_text}" if fallback_text else onboarding_message()
                    buf.append_onboarding = False
                buf.append_message("assistant", fallback_text)
            # Then post to Slack
            web.chat_postMessage(channel=channel, text=fallback_text)
            _snippet = (raw_content or "")
            log.info(f"Posted reply: {_snippet[:80]}...")

        # No immediate save here; background worker will persist shortly
        log.info(f"[FINAL STATE] Updated in-memory state successfully for channel {channel}")

    except Exception as e:
        log.error(f"Error handling message: {e}")
        # Post an error message
        try:
            web.chat_postMessage(
                channel=channel,
                text="❌ Sorry, I encountered an error processing your message.",
            )
        except Exception as post_error:
            log.error(f"Failed to post error message: {post_error}")


def clear_history(channel):
    # Drop in-memory buffer
    with _channel_map_lock:
        if channel in channel_buffers:
            try:
                with channel_buffers[channel].lock():
                    channel_buffers[channel]._dump_queue("clear_history", "before delete")
            except Exception:
                pass
            del channel_buffers[channel]
    # Cancel any pending timer
    if channel in pending_responses:
        try:
            timer = pending_responses[channel].get("timer")
            if timer:
                timer.cancel()
        except Exception:
            pass
        del pending_responses[channel]
    try:
        if conversations_collection is not None:
            conversations_collection.delete_one({"channel_id": channel})
    except Exception as e:
        log.warning(f"Failed to delete conversation from DB: {e}")
    web.chat_postMessage(
        channel=channel, text="✅ Conversation cleared. Starting fresh."
    )



@app.route("/slack/events", methods=["POST"])
def slack_events():
    """Handle incoming Slack events"""
    # Get request data
    data = request.get_json(silent=True) or {}

    # Verify request signature
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    request_body = request.get_data(as_text=True)

    if not verify_slack_signature(request_body, timestamp, signature, SLACK_SIGNING_SECRET):
        log.warning("Invalid signature")
        return jsonify({"error": "Invalid signature"}), 403

    # Handle URL verification challenge
    if data.get("type") == "url_verification":
        log.info("Responding to URL verification challenge")
        challenge = data.get("challenge")
        if challenge:
            return jsonify({"challenge": challenge})
        return jsonify({"error": "missing challenge"}), 400

    # Handle events
    if data.get("type") == "event_callback":
        event = data.get("event") or {}

        # Process message events in a background thread
        if event.get("type") == "message":
            thread = threading.Thread(target=handle_message, args=(event,))
            thread.start()

        # Respond quickly to Slack
        return jsonify({"status": "ok"})

    return jsonify({"status": "ignored"})


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "bot_user_id": BOT_USER_ID})


@app.route("/", methods=["GET"])
def home():
    """Home endpoint"""
    return jsonify(
        {"status": "running", "bot_name": "Slack Chatbot", "bot_user_id": BOT_USER_ID}
    )


_analysis_lock = threading.Lock()
_analysis_running = False


def _run_conversation_analysis_background(source="slack"):
    global _analysis_running
    try:
        from commons.analyze_conversations import ConversationAnalyzer
        from commons.db import get_db
        import csv

        log.info(f"[ANALYSIS] Background conversation analysis started for source: {source}.")
        analyzer = ConversationAnalyzer()

        if not analyzer.mongo_client:
            log.error("[ANALYSIS] MongoDB connection not available.")
            return

        db_ref = get_db()

        if source == "survey":
            csv_file_path = "learning_bot/survey11_06_2026.csv"

            if not os.path.exists(csv_file_path):
                log.error(f"[ANALYSIS] Survey file not found at {csv_file_path}")
                return

            log.info("[ANALYSIS] Parsing single-user survey data into conversation format...")
            with open(csv_file_path, mode='r', encoding='utf-8-sig') as file:
                reader = csv.reader(file)
                rows = list(reader)

                if len(rows) >= 4:
                    questions_row = rows[1]  # Row 2 (Index 1)
                    answers_row = rows[3]    # Row 4 (Index 3)

                    messages = []

                    # Start at index 17 to skip Qualtrics metadata (IP, dates, duration, etc.)
                    start_column_index = 17

                    for i in range(start_column_index, min(len(questions_row), len(answers_row))):
                        q_text = questions_row[i].strip()
                        a_text = answers_row[i].strip()

                        if q_text and a_text:
                            messages.append({"role": "assistant", "content": q_text})
                            messages.append({"role": "user", "content": a_text})

                    if messages:
                        db_ref.conversations.update_one(
                            {"channel_id": "survey_channel_chris"},
                            {
                                "$set": {
                                    "user_id": "survey_chris",
                                    "source_type": "survey",
                                    "messages": messages,
                                    "updated_at": time.time()
                                }
                            },
                            upsert=True
                        )
                        log.info(f"[ANALYSIS] Parsed {len(messages)//2} Q&A pairs for the single user.")
                    else:
                        log.warning("[ANALYSIS] No valid question-answer pairs found.")
                else:
                    log.error("[ANALYSIS] CSV does not have the expected 4 rows.")

        analyzer.process_all_conversations()

        total_updated = db_ref.conversations.count_documents(
            {"style_rules": {"$exists": True}}
        )
        log.info(
            f"[ANALYSIS] Background analysis finished. "
            f"Users with style rules: {total_updated}"
        )
    except Exception as e:
        log.exception(f"[ANALYSIS] Error in background conversation analysis: {e}")
    finally:
        # 작업 끝나면 running 플래그 내려주기
        with _analysis_lock:
            _analysis_running = False


@app.route("/analyze-conversations", methods=["GET", "POST"])
def analyze_conversations():
    global _analysis_running

    source = "slack"
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        source = data.get("source", "slack")

    with _analysis_lock:
        if _analysis_running:
            return jsonify(
                {"status": "already_running", "message": "Analysis is already in progress"}
            ), 202

        _analysis_running = True
        t = threading.Thread(
            target=_run_conversation_analysis_background,
            args=(source,),
            daemon=True,
        )
        t.start()

    return jsonify(
        {
            "status": "started",
            "message": f"Conversation analysis started in background for source: {source}",
        }
    ), 202


# ---- Gap question review session tracking ----------------------------------
# maps channel_id -> {"user_id": str, "questions": [...], "current_idx": int}
_gap_sessions: Dict[str, Dict[str, Any]] = {}
_gap_lock = threading.Lock()


def _start_gap_session(channel: str, user_id: str, questions: List[Dict[str, Any]]):
    """Start a gap review session and send the first question."""
    log.info(f"[GAP-REVIEW] Starting session for channel={channel} user={user_id} questions={len(questions)}")
    with _gap_lock:
        _gap_sessions[channel] = {
            "user_id": user_id,
            "questions": questions,
            "current_idx": 0,
        }
    log.info(f"[GAP-REVIEW] Session stored. Active sessions: {list(_gap_sessions.keys())}")
    _send_next_gap_question(channel)


def _send_next_gap_question(channel: str):
    """Send the next gap question or end the session if done."""
    with _gap_lock:
        session = _gap_sessions.get(channel)
        if not session:
            return
        idx = session["current_idx"]
        questions = session["questions"]

    if idx >= len(questions):
        # all done
        with _gap_lock:
            _gap_sessions.pop(channel, None)
        web.chat_postMessage(
            channel=channel,
            text="That's all the questions! Thanks for helping your proxy get smarter."
        )
        return

    question = questions[idx]["question"]
    remaining = len(questions) - idx
    web.chat_postMessage(
        channel=channel,
        text=(
            f"({remaining} question{'s' if remaining != 1 else ''} remaining) "
            f"Someone asked your proxy:\n\n*{question}*\n\n"
            f"Reply with your answer, or type *skip* to skip this one."
        )
    )


def _handle_gap_response(channel: str, text: str) -> bool:
    """Handle a user's response during a gap review session.

    Returns True if this message was consumed by the gap session,
    False if there's no active session (message should go to normal flow).
    """
    with _gap_lock:
        session = _gap_sessions.get(channel)
        if not session:
            log.info(f"[GAP-REVIEW] No active session for channel={channel}")
            return False
        user_id = session["user_id"]
        idx = session["current_idx"]
        questions = session["questions"]

    log.info(f"[GAP-REVIEW] Handling response for channel={channel} idx={idx} text={text[:80]}")
    current_gap = questions[idx]
    question_text = current_gap["question"]

    uq_col = get_unanswered_questions_collection()
    stripped = text.strip().lower()

    if stripped == "skip":
        # user chose to skip — mark as skipped
        log.info(f"[GAP-REVIEW] User {user_id} skipped: {question_text}")
        if uq_col is not None:
            try:
                uq_col.update_one(
                    {"partner_id": user_id, "question": question_text},
                    {"$set": {"skipped": True, "updated_at": time.time()}}
                )
            except Exception as e:
                log.warning(f"[GAP-REVIEW] Failed to mark as skipped: {e}")
    else:
        # user provided an answer — store it
        log.info(f"[GAP-REVIEW] User {user_id} answered: {question_text}")

        # save the answer to the conversations collection
        conv_col = get_conversations_collection()
        if conv_col is not None:
            try:
                conv_col.update_one(
                    {"user_id": user_id},
                    {
                        "$push": {
                            "messages": {
                                "$each": [
                                    {"role": "assistant", "content": question_text},
                                    {"role": "user", "content": text},
                                ]
                            }
                        }
                    },
                    upsert=False,
                )
            except Exception as e:
                log.warning(f"[GAP-REVIEW] Failed to append to conversations: {e}")

        # mark as answered in unanswered_questions
        if uq_col is not None:
            try:
                uq_col.update_one(
                    {"partner_id": user_id, "question": question_text},
                    {"$set": {
                        "answered": True,
                        "answer": text,
                        "answered_at": time.time(),
                    }}
                )
            except Exception as e:
                log.warning(f"[GAP-REVIEW] Failed to mark as answered: {e}")

        web.chat_postMessage(channel=channel, text="Got it, thanks!")

    # advance to next question
    with _gap_lock:
        session = _gap_sessions.get(channel)
        if session:
            session["current_idx"] += 1

    _send_next_gap_question(channel)
    return True


@app.route("/review-gaps/<user_id>", methods=["GET", "POST"])
def review_gaps(user_id):
    """Trigger the learning bot to ask unanswered questions for a given user.

    Fetches questions from the unanswered_questions collection where
    answered=False and not skipped, then DMs the user one at a time.
    """
    uq_col = get_unanswered_questions_collection()
    if uq_col is None:
        return jsonify({"error": "unanswered_questions collection not available"}), 500

    # find all unanswered, unskipped gaps for this user
    gaps = list(uq_col.find({
        "partner_id": user_id,
        "answered": False,
        "skipped": {"$ne": True},
    }))
    if not gaps:
        return jsonify({"status": "no_gaps", "user_id": user_id, "message": "No unanswered questions found."}), 200

    questions = [{"question": g["question"], "category": g.get("category", "unknown")} for g in gaps]

    def _start():
        try:
            dm_resp = web.conversations_open(users=[user_id])
            channel = dm_resp["channel"]["id"]
            _start_gap_session(channel, user_id, questions)
        except Exception as e:
            log.error(f"[GAP-REVIEW] Error starting gap review for {user_id}: {e}")

    t = threading.Thread(target=_start, daemon=True)
    t.start()

    return jsonify({
        "status": "started",
        "user_id": user_id,
        "questions_count": len(questions),
        "questions": [g["question"] for g in questions],
    }), 202


# ---- Cornell ID registration -----------------------------------------------
# tracks channels where we've asked for a cornell ID and are waiting for a response
_cornell_id_pending: Dict[str, str] = {}  # channel -> user_id
_cornell_lock = threading.Lock()

# cache of user_ids that we know already have a cornell_id in the DB
_cornell_id_cache: set = set()


def _user_has_cornell_id(channel: str, user_id: str) -> bool:
    """Check if this user already has a cornell_id stored in the DB."""
    # check cache first
    if user_id in _cornell_id_cache:
        return True

    # check if we're already prompting this channel
    with _cornell_lock:
        if channel in _cornell_id_pending:
            return True  # we're mid-prompt, don't re-prompt

    # check the DB
    conv_col = get_conversations_collection()
    if conv_col is not None:
        try:
            doc = conv_col.find_one(
                {"$or": [{"user_id": user_id}, {"channel_id": channel}]},
                {"cornell_id": 1}
            )
            if doc and doc.get("cornell_id"):
                _cornell_id_cache.add(user_id)
                return True
        except Exception as e:
            log.warning(f"[CORNELL-ID] Failed to check cornell_id for {user_id}: {e}")

    return False


def _prompt_for_cornell_id(channel: str, user_id: str):
    """Send a message asking the user for their Cornell ID."""
    with _cornell_lock:
        _cornell_id_pending[channel] = user_id

    web.chat_postMessage(
        channel=channel,
        text="Hey! Before we get started, what's your Cornell ID? (e.g., abc123)"
    )
    log.info(f"[CORNELL-ID] Prompted user {user_id} in channel {channel}")


def _handle_cornell_id_response(channel: str, user_id: str, text: str) -> bool:
    """Handle the user's cornell ID response.

    If a document with this cornell_id already exists (from a prior survey
    submission), attach channel_id and user_id to it. Otherwise create a new one.

    Returns True if this message was consumed, False otherwise.
    """
    with _cornell_lock:
        if channel not in _cornell_id_pending:
            return False

    from commons.spc_pipeline import normalize_cornell_id
    cornell_id = normalize_cornell_id(text)

    # basic validation — cornell IDs are typically letters + numbers, short
    if not cornell_id or len(cornell_id) > 20:
        web.chat_postMessage(
            channel=channel,
            text="That doesn't look like a valid Cornell ID. Please try again (e.g., abc123)."
        )
        return True

    # store it in the DB
    conv_col = get_conversations_collection()
    if conv_col is not None:
        try:
            # check if a document with this cornell_id already exists
            # (created by a prior survey submission via /spc/ingest)
            existing = conv_col.find_one({"cornell_id": cornell_id})

            if existing and not existing.get("channel_id"):
                # survey-first flow: document exists from SPC ingest but has
                # no channel_id yet. attach this channel and user to it.
                conv_col.update_one(
                    {"cornell_id": cornell_id},
                    {
                        "$set": {
                            "channel_id": channel,
                            "user_id": user_id,
                        },
                        "$setOnInsert": {
                            "messages": [{"role": "system", "content": get_learning_bot_prompt()}],
                        },
                    },
                )
                log.info(f"[CORNELL-ID] Attached channel={channel} to existing survey doc for cornell_id={cornell_id}")
            else:
                # bot-first flow: no existing survey doc, or doc already has a channel.
                # upsert on channel_id as before.
                conv_col.update_one(
                    {"channel_id": channel},
                    {
                        "$set": {"cornell_id": cornell_id, "user_id": user_id},
                        "$setOnInsert": {
                            "messages": [{"role": "system", "content": get_learning_bot_prompt()}],
                        },
                    },
                    upsert=True,
                )
                log.info(f"[CORNELL-ID] Stored cornell_id={cornell_id} for user {user_id} channel={channel}")

        except Exception as e:
            log.error(f"[CORNELL-ID] Failed to store cornell_id: {e}")
            web.chat_postMessage(channel=channel, text="Something went wrong saving your ID, try again later.")
            return True

    _cornell_id_cache.add(user_id)

    with _cornell_lock:
        _cornell_id_pending.pop(channel, None)

    # send the onboarding greeting so the interview starts immediately
    web.chat_postMessage(
        channel=channel,
        text=onboarding_message()
    )

    # clear the append_onboarding flag so it doesn't get sent again
    # with the first question response
    try:
        buf = get_or_create_buffer(channel)
        with buf.lock():
            buf.append_onboarding = False
    except Exception:
        pass

    return True


# ---- SPC Survey Ingest Endpoint ---------------------------------------------

@app.route("/spc/ingest", methods=["POST"])
def spc_ingest():
    """Receive SPC survey data from Qualtrics webhook and generate personality.

    Accepts the Qualtrics webhook JSON payload directly. The payload must
    include cornell_id along with bfi_* and pvq_* fields.
    """
    from commons.spc_pipeline import ingest_spc_from_webhook, normalize_cornell_id

    data = request.get_json(silent=True) or {}

    cornell_id = normalize_cornell_id(data.get("cornell_id", ""))
    if not cornell_id:
        return jsonify({"error": "cornell_id is required"}), 400

    def _run_ingest():
        try:
            result = ingest_spc_from_webhook(data, oai, MODEL)
            log.info(f"[SPC] Ingest result for {cornell_id}: {result}")
        except Exception as e:
            log.error(f"[SPC] Ingest failed for {cornell_id}: {e}")

    t = threading.Thread(target=_run_ingest, daemon=True)
    t.start()

    return jsonify({
        "status": "started",
        "cornell_id": cornell_id,
        "message": "SPC personality generation started in background.",
    }), 202


if __name__ == "__main__":
    log.info("Slack bot with HTTPS events and MongoDB storage started")

    # Get port from environment (Heroku sets this)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
    