import datetime
import json
import logging
import os
import re
import threading
from typing import Dict, Any, List, Optional, Tuple, Set, Literal

from pydantic import BaseModel

from commons.clients import make_slack_client, make_openai_client, get_openai_model
from commons.db import (
    get_mongo_client,
    get_db,
    get_conversations_collection,
    get_proxy_collection,
    get_partner_maps_collection,
    get_unanswered_questions_collection,
)
from proxy_bot.proxy_bot_mimic_prompts import PROXY_BOT_MIMIC_PROMPT, PROXY_BOT_FREE_PROMPT
from proxy_bot.proxy_bot_prompts import PROXY_BOT_PROMPT
from proxy_bot.question_categories import MIMIC_CLASSIFICATION_PROMPT, STRICT_CLASSIFICATION_PROMPT, FREE_CLASSIFICATION_PROMPT

# ---- Logging --------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("proxy_bot_service")

# ---- Clients --------------------------------------------------------------
web, BOT_USER_ID = make_slack_client("PROXY_SLACK_BOT_TOKEN")
oai = make_openai_client()


class ProxyResponse(BaseModel):
    """Schema for the classification + reply envelope returned by the LLM.

    Mirrors the OUTPUT FORMAT contract in proxy_bot/question_categories.py.
    Used with oai.responses.parse(text_format=...) to enforce schema-valid
    JSON at the API level rather than parsing free-form text.
    """
    category: Literal[
        "identity", "preference", "experiential", "decision", "non_question"
    ]
    action: Literal["answer", "infer", "deflect", "defer"]
    has_prior_knowledge: bool
    confidence: Literal["high", "medium", "low"]
    extracted_question: Optional[str]
    response: str


OPENAI_MODEL = get_openai_model("PROXY_MODEL", "gpt-5.4")

# ---- MongoDB (singleton helpers) -----------------------------------------
mongo_client = get_mongo_client()
db = get_db() if mongo_client is not None else None
conversations_collection = get_conversations_collection() if db is not None else None
proxy_collection = get_proxy_collection() if db is not None else None
if proxy_collection is not None:
    log.info("Proxy bot connected to MongoDB proxy collection (singleton)")
else:
    log.warning(
        "MongoDB proxy collection not configured; conversation history unavailable"
    )
if conversations_collection is not None:
    log.info("Proxy bot can access conversations collection for partner context")
else:
    log.warning(
        "MongoDB conversations not configured; partner style/history may be unavailable"
    )

seen_events = set()

# Track newest user message timestamp per channel to drop stale replies
_latest_user_ts: Dict[str, float] = {}
_ts_lock = threading.RLock()

channel_state: Dict[str, Dict[str, Any]] = {}

# ---- Background persistence of the single per-channel conversation ----------
_conv_cursors: Dict[str, int] = {}
_persist_lock = threading.RLock()


def _conversation_persist_loop(interval_seconds: float = 1.0):
    while True:
        try:
            if proxy_collection is not None:
                with _persist_lock:
                    items = list(channel_state.items())
                for channel_id, state in items:
                    try:
                        convo: List[Dict[str, Any]] = state.get("conversation", [])
                        saved_len = _conv_cursors.get(channel_id, 0)
                        if len(convo) <= saved_len:
                            continue
                        proxy_collection.update_one(
                            {"channel_id": channel_id},
                            {
                                "$set": {
                                    "conversation": convo,
                                    "updated_at": datetime.datetime.utcnow(),
                                },
                                "$setOnInsert": {"created_at": datetime.datetime.utcnow()},
                            },
                            upsert=True,
                        )
                        _conv_cursors[channel_id] = len(convo)
                    except Exception as e:
                        log.error(f"Failed to persist conversation for {channel_id}: {e}")
        except Exception as e:
            log.error(f"Conversation persist loop error: {e}")
        finally:
            try:
                import time as _t
                _t.sleep(interval_seconds)
            except Exception:
                pass


# ---- Clear History -----------------------------------------------------------
def clear_proxy_history(channel: str):
    """Clear proxy bot conversation history from both local memory and MongoDB."""
    # Remove from in-memory state
    if channel in channel_state:
        del channel_state[channel]
        log.info(f"Cleared in-memory state for channel {channel}")

    # Reset persist cursor so new conversations get saved
    with _persist_lock:
        _conv_cursors.pop(channel, None)

    # Delete from MongoDB proxy collection
    try:
        if proxy_collection is not None:
            result = proxy_collection.delete_one({"channel_id": channel})
            if result.deleted_count > 0:
                log.info(
                    f"Deleted proxy conversation from MongoDB for channel {channel}"
                )
            else:
                log.info(
                    f"No proxy conversation found in MongoDB for channel {channel}"
                )
    except Exception as e:
        log.warning(f"Failed to delete proxy conversation from MongoDB: {e}")

    # Send confirmation message
    try:
        web.chat_postMessage(
            channel=channel, text="✅ Proxy conversation cleared. Starting fresh."
        )
    except Exception as e:
        log.error(f"Failed to send clear confirmation message: {e}")


# ---- MongoDB Persistence ------------------------------------------------------
def save_proxy_initial_state(channel_id: str, requester_user_id: str, state: Dict[str, Any]):
    """Save proxy bot conversation state to MongoDB proxy collection."""
    if proxy_collection is None:
        log.warning("MongoDB proxy collection not available; skipping save")
        return

    try:
        oa_messages = state.get("oa_messages", [])
        doc = {
            "channel_id": channel_id,
            "user_id": requester_user_id,
            "style_rules": state.get("style_rules"),
            "sample_messages": state.get("sample_messages", []),
            "oa_messages": oa_messages,
            "user_name": get_user_profile_name(requester_user_id),
            "conversation": state.get("conversation", []),
            "updated_at": datetime.datetime.utcnow(),
        }

        proxy_collection.update_one(
            {"channel_id": channel_id},
            {"$set": doc, "$setOnInsert": {"created_at": datetime.datetime.utcnow()}},
            upsert=True,
        )

        log.info(
            f"✓ Saved {len(oa_messages)} messages to MongoDB for channel {channel_id}"
        )

    except Exception as e:
        log.error(
            f"✗ Failed to save conversation to MongoDB for channel {channel_id}: {e}"
        )


def filter_user_messages(messages: List[str]) -> List[str]:
    """
    Filters a list of sample messages, keeping only those starting with 'user:' 
    and excluding those starting with 'Question:'.
    """
    if not isinstance(messages, list):
        return []
    return [msg for msg in messages if isinstance(msg, str) and msg.startswith("user:")]


def load_proxy_conversation(channel_id: str) -> Optional[Dict[str, Any]]:
    """Load proxy bot conversation state from MongoDB proxy collection."""
    if proxy_collection is None:
        return None

    try:
        doc = proxy_collection.find_one({"channel_id": channel_id})

        if doc:
            state = {
                "style_rules": doc.get("style_rules"),
                "sample_messages": filter_user_messages(doc.get("sample_messages", [])),
                "oa_messages": doc.get("oa_messages", []),
                # Load persisted conversation history if present
                "conversation": doc.get("conversation", []),
            }
            return state
        
        # If no existing conversation is found, the user is starting fresh.
        # Ensure we have the absolute latest partner map from the DB before initializing.
        load_partner_map_from_db()
        return None
    except Exception as e:
        log.error(f"Failed to load proxy conversation: {e}")
        return None


# Global in-memory partner map shared by proxy modules
PARTNER_MAP: Dict[str, Dict[str, str]] = {}


# ---- Users file (for name->id resolution; load on demand per request) -------
def _normalize_name(name: str) -> str:
    try:
        # Collapse whitespace, strip, lowercase
        s = re.sub(r"\s+", " ", (name or "").strip()).lower()
        # Optionally also keep a variant without parenthetical for more forgiving matches
        return s
    except Exception:
        return (name or "").strip().lower()


def _users_json_path() -> str:
    # users.json is at repo root; this file lives in proxy_bot/
    here = os.path.dirname(__file__)
    return os.path.abspath(os.path.join(here, "..", "users.json"))


def _load_users_index() -> Dict[str, Set[str]]:
    """Load users.json and build a name->user_ids index for real/display names.

    Called only by the /set_partners flow; does not cache in memory.
    """
    path = _users_json_path()
    name_to_ids: Dict[str, Set[str]] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for entry in data if isinstance(data, list) else []:
            try:
                uid = str(entry.get("user_id") or "").strip()
                if not uid:
                    continue
                real_name = entry.get("real_name") or ""
                display_name = entry.get("display_name") or ""

                for raw in (real_name, display_name):
                    n = _normalize_name(raw)
                    if not n:
                        continue
                    s = name_to_ids.get(n)
                    if s is None:
                        s = set()
                        name_to_ids[n] = s
                    s.add(uid)
            except Exception:
                continue
        log.info(
            f"Built users index from users.json with {sum(len(v) for v in name_to_ids.values())} entries"
        )
    except Exception as e:
        log.warning(f"Could not load users.json for name->id mapping: {e}")
    return name_to_ids


_USER_ID_RE = re.compile(r"^U[A-Z0-9]+$")


def _resolve_user_id_from_input(token: str, name_to_ids: Dict[str, Set[str]]) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a CSV token (expected username) to Slack user_id.

    Returns (user_id, error_str). If ambiguous or not found, user_id=None and error contains reason.
    Accepts either a real/display name (preferred) or a literal Slack user id as fallback.
    """
    tok = (token or "").strip()
    if not tok:
        return None, "empty token"
    # If it looks like a user id already, accept as-is
    if _USER_ID_RE.match(tok) or tok.startswith("survey_"):
        return tok, None
    # Else, treat as name and look up
    key = _normalize_name(tok)
    ids = name_to_ids.get(key)
    if not ids:
        return None, f"unknown name '{token}'"
    if len(ids) > 1:
        return None, f"ambiguous name '{token}' maps to {sorted(ids)}"
    return next(iter(ids)), None


def load_partner_map_from_db() -> bool:
    col = get_partner_maps_collection()
    if col is None:
        return False
    try:
        doc = col.find_one({"_id": "partner_map"})
        if not doc or not isinstance(doc.get("map"), dict):
            return False
        raw_map = doc.get("map", {})
        # Validate and normalize
        normalized: Dict[str, Dict[str, str]] = {}
        for k, v in raw_map.items():
            if isinstance(v, dict):
                pid = v.get("partner_id") or v.get("partner") or v.get("id")
                mode = (v.get("mode") or "mimic").lower()
                if mode not in ("strict", "mimic", "free"):
                    mode = "mimic"
                if pid:
                    normalized[str(k)] = {"partner_id": str(pid), "mode": mode}
            else:
                normalized[str(k)] = {"partner_id": str(v), "mode": "mimic"}
        if normalized:
            global PARTNER_MAP
            PARTNER_MAP = normalized
            _log_partner_map_stats("loaded_from_db")
            return True
    except Exception as e:
        log.error(f"Failed loading partner map from DB: {e}")
    return False


def save_partner_map_to_db() -> bool:
    col = get_partner_maps_collection()
    if col is None:
        return False
    try:
        col.update_one(
            {"_id": "partner_map"},
            {"$set": {"map": PARTNER_MAP, "updated_at": datetime.datetime.utcnow()}},
            upsert=True,
        )
        _log_partner_map_stats("saved_to_db")
        return True
    except Exception as e:
        log.error(f"Failed saving partner map to DB: {e}")
        return False

def process_clear_mode_if_requested(text: str) -> str:
    """Parses text for the -s flag. If present, clears proxy histories for users in the payload.
    Returns the cleaned text to be inserted into the partner map, without the -s flag.
    Raises Exception if clear fails.
    """
    if text.startswith("-s,"):
        text = text[3:].strip()
    elif text.startswith("-s "):
        text = text[3:].strip()
    else:
        return text

    name_to_ids = _load_users_index()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    user_ids_to_clear = []
    
    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 1:
            parts = line.split()
        if len(parts) >= 2:
            user_id, _ = _resolve_user_id_from_input(parts[0], name_to_ids)
            if user_id:
                user_ids_to_clear.append(user_id)
    
    for uid in user_ids_to_clear:
        channel_id = ""
        if proxy_collection is not None:
            doc = proxy_collection.find_one({"user_id": uid})
            if doc and "channel_id" in doc:
                channel_id = doc["channel_id"]
        if not channel_id:
            try:
                resp = web.conversations_open(users=[uid])
                if resp.get("ok"):
                    channel_id = resp["channel"]["id"]
            except Exception:
                pass
        if channel_id:
            from commons.history import archive_and_clear_history
            archive_and_clear_history(channel_id, clear_proxy=True, clear_learning=False)
            
    return text


def update_partner_map_from_csv(csv_text: str) -> Tuple[int, List[str]]:
    """Parse CSV-like lines 'user_name,partner_name,mode' and update PARTNER_MAP.

    - user_name and partner_name should be either real_name or display_name.
    - As a fallback, literal Slack user IDs like 'U0ABC...' are accepted.
    Returns (updated_count, errors)
    """
    global PARTNER_MAP
    updated = 0
    errors: List[str] = []
    if not csv_text:
        return 0, ["empty payload"]

    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    # Build name->id index for this request only (no in-memory caching)
    name_to_ids = _load_users_index()
    for idx, line in enumerate(lines, start=1):
        try:
            # Allow either comma or whitespace-separated; prefer comma
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 1:
                parts = line.split()
            if len(parts) < 2:
                errors.append(f"line {idx}: expected 2-3 columns, got '{line}'")
                continue
            # Resolve names to user_ids
            user_id, err1 = _resolve_user_id_from_input(parts[0], name_to_ids)
            partner_id, err2 = _resolve_user_id_from_input(parts[1], name_to_ids)
            if err1:
                errors.append(f"line {idx}: {err1}")
            if err2:
                errors.append(f"line {idx}: {err2}")
            if not user_id or not partner_id:
                continue
            mode = parts[2].lower() if len(parts) >= 3 else "mimic"
            if mode not in ("strict", "mimic", "free"):
                errors.append(f"line {idx}: invalid mode '{mode}', expected 'strict', 'mimic', or 'free'")
                mode = "mimic"
            PARTNER_MAP[str(user_id)] = {"partner_id": str(partner_id), "mode": mode}
            updated += 1
        except Exception as e:
            errors.append(f"line {idx}: {e}")

    _log_partner_map_stats("update_partner_map_from_csv")
    save_partner_map_to_db()
    return updated, errors


def resolve_partner(user_id: str) -> Optional[str]:
    """Return the partner's Slack user_id for a given user_id, if configured."""
    entry = PARTNER_MAP.get(user_id)
    if entry is None:
        return "U09DUT0FJNS"  # fall back to Jieun
    if isinstance(entry, dict):
        return entry.get("partner_id")
    # Backward-compat if map contains plain string
    return str(entry)


def resolve_partner_mode(user_id: str) -> Optional[str]:
    entry = PARTNER_MAP.get(user_id)
    if isinstance(entry, dict):
        return entry.get("mode")
    return "mimic"


def _partner_map_stats() -> Tuple[int, int, int, int]:
    """Return (total, strict, mimic, invalid)."""
    total = len(PARTNER_MAP)
    strict = 0
    mimic = 0
    invalid = 0
    for v in PARTNER_MAP.values():
        if isinstance(v, dict):
            mode = (v.get("mode") or "").lower()
            if mode == "strict":
                strict += 1
            elif mode == "mimic":
                mimic += 1
            else:
                invalid += 1
        else:
            invalid += 1
    return total, strict, mimic, invalid


def _log_partner_map_stats(context: str) -> None:
    total, strict, mimic, invalid = _partner_map_stats()
    log.info(
        f"[PARTNER_MAP] {context}: total={total} strict={strict} mimic={mimic} invalid={invalid}"
    )


# Log stats at import/init and attempt to load persisted map
if not load_partner_map_from_db():
    _log_partner_map_stats("init")

# Start background persistence thread for conversation arrays
_conv_thread = threading.Thread(target=_conversation_persist_loop, args=(1.0,), daemon=True)
_conv_thread.start()


def get_user_profile_name(user_id: str) -> str:
    """Fetch a display name for a Slack user_id (best-effort)."""
    if user_id == "U09DUT0FJNS":
        return "Jieun Kim"
    if user_id.startswith("survey_"):
        return "Survey Chris Johnson"
    try:
        resp = web.users_info(user=user_id)
        if resp.get("ok"):
            user_obj = resp.get("user") or {}
            prof = user_obj.get("profile", {}) if isinstance(user_obj, dict) else {}
            return (
                    (prof.get("display_name") if isinstance(prof, dict) else None)
                    or (prof.get("real_name") if isinstance(prof, dict) else None)
                    or (user_obj.get("name") if isinstance(user_obj, dict) else None)
                    or user_id
            )
    except Exception as e:
        log.warning(f"Failed to fetch profile for {user_id}: {e}")
    return user_id


def _extract_user_messages(
        messages: List[Dict[str, Any]], max_count: int = 40, max_chars: int = 6000
) -> List[str]:
    """Return recent user-authored message contents, capped for prompt size."""
    user_msgs = [
        ("Question: " if m.get("role", "") == "assistant" else "user: ") + m.get("content", "")
        for m in messages
        if m.get("content") and (m.get("role") == "user" or m.get("role") == "assistant")
    ]
    return user_msgs


def fetch_partner_context(
        partner_user_id: str,
) -> Tuple[Optional[Dict[str, Any]], List[str], str, str, Dict[str, str]]:
    """Load partner's most recent conversation doc and style from Mongo.

    Returns: (style_rules_dict_or_None, sample_user_messages, partner_user_name, personality_details, spc_context)
    """
    if conversations_collection is None:
        return None, [], get_user_profile_name(partner_user_id), "", {}

    try:
        doc = (
            conversations_collection.find({"user_id": partner_user_id})
            .sort("updated_at", -1)
            .limit(1)
        )
        doc = next(iter(doc), None)
        if not doc:
            return None, [], get_user_profile_name(partner_user_id), "", {}

        style_rules = doc.get("style_rules")
        personality_details = doc.get("personality",
                              "You have a balanced and adaptable personality. You can be social when the situation calls for it but also enjoy your own company. You're generally open to new ideas and experiences, cooperative in your interactions, and handle stress reasonably well.")

        # load SPC context (loves, hates, routines) if available
        spc_raw = doc.get("spc_raw", {})
        spc_context = spc_raw.get("context", {}) if isinstance(spc_raw, dict) else {}

        messages = doc.get("messages", [])
        samples = _extract_user_messages(messages)
        partner_user_name = get_user_profile_name(
            partner_user_id
        )
        return style_rules, samples, partner_user_name, personality_details, spc_context
    except Exception as e:
        log.error(f"Error fetching partner context: {e}")
        return None, [], get_user_profile_name(partner_user_id), "", {}

def build_system_prompt(
        partner_name: str,
        partner_id: str,
        style_rules: Optional[Dict[str, Any]],
        sample_messages: List[str],
        personality_details: str = "",
        spc_context: Optional[Dict[str, str]] = None,
        rules_prompt: str = PROXY_BOT_PROMPT,
        mode: str = "mimic"
) -> str:
    """Create a system prompt instructing the model to mimic the partner's style."""
    style_snippet = ""
    if style_rules:
        # Keep the most informative parts compact
        condensed = {
            k: style_rules.get(k)
            for k in [
                "summary",
                "formality",
                "cadence",
                "punctuation",
                "emoji_and_markers",
                "lexicon",
                "style_rules_do",
                "style_rules_dont",
                "safety_note",
            ]
            if k in style_rules
        }
        try:
            style_snippet = json.dumps(condensed, ensure_ascii=False)
        except Exception:
            style_snippet = str(condensed)

    samples_text = "\n- ".join(sample_messages) if sample_messages else ""

    # build personal context section from SPC survey answers
    context_section = ""
    if spc_context:
        parts = []
        if spc_context.get("loves"):
            parts.append(f"Things you love: {spc_context['loves']}")
        if spc_context.get("hates"):
            parts.append(f"Things you hate: {spc_context['hates']}")
        if spc_context.get("weekday"):
            parts.append(f"Your typical weekday: {spc_context['weekday']}")
        if spc_context.get("weekend"):
            parts.append(f"Your typical weekend: {spc_context['weekend']}")
        if parts:
            context_section = "YOUR PERSONAL LIFE CONTEXT:\n" + "\n".join(parts) + "\n\n"

    if mode == "free":
        classification_prompt = FREE_CLASSIFICATION_PROMPT
    elif mode == "mimic":
        classification_prompt = MIMIC_CLASSIFICATION_PROMPT
    else:
        classification_prompt = STRICT_CLASSIFICATION_PROMPT

    prompt = (
        f"You are {partner_name} chatting on Slack.\n"
        f"Respond naturally as yourself, not as an assistant or helper.\n"
        f"Use your natural tone, vocabulary, punctuation, and conversational style.\n\n"
        f"YOUR COMMUNICATION STYLE:\n{style_snippet}\n\n"
        f"YOUR PERSONALITY:\n{personality_details}\n\n"
        f"{context_section}"
        f"[YOUR_CONVERSATION_HISTORY] - This is from your past conversations (you are the user):\n\n"
        f"{samples_text}\n\n"
        f"Use these conversations to know what you've shared about yourself before.\n"
        f"YOU MUST SAY who you are (your FULL NAME) when greeting ONLY THE FIRST TIME.\n\n"
        f"IMPORTANT RULES:\n{rules_prompt}\n\n"
        f"{classification_prompt}\n"
    )
    return prompt


def init_channel_state(
        channel_id: str, requester_user_id: str, talk_to_myself: bool = False
) -> Optional[Dict[str, Any]]:
    """Initialize channel state by looking up the requester's partner (or themselves) and building a prompt."""
    if talk_to_myself:
        partner_id = requester_user_id
        # When talking to yourself, force mimic mode to copy your own style, 
        # or just fallback to what you have configured. We'll force mimic for self-chat.
        mode = "mimic"
    else:
        partner_id = resolve_partner(requester_user_id)
        mode = (resolve_partner_mode(requester_user_id) or "mimic").lower()

    style_rules, samples, partner_name, personality_details, spc_context = fetch_partner_context(partner_id)
    rules = PROXY_BOT_FREE_PROMPT if mode == "free" else PROXY_BOT_MIMIC_PROMPT if mode == "mimic" else PROXY_BOT_PROMPT
    system_prompt = build_system_prompt(partner_name, partner_id, style_rules, samples, personality_details, spc_context, rules, mode)

    state = {
        "style_rules": style_rules,
        "sample_messages": samples,
        # oa_messages is the static prompt stack (system, optional priors)
        "oa_messages": [{"role": "system", "content": system_prompt}],
        # conversation holds user/assistant history (persisted via queue)
        "conversation": [],
    }
    channel_state[channel_id] = state
    # Save initial state to MongoDB
    save_proxy_initial_state(channel_id, requester_user_id, state)

    return state


def _get_or_create_channel_state(
        channel: str, requester_user_id: str, talk_to_myself: bool
) -> Optional[Dict[str, Any]]:
    """Fetch existing state from memory, then DB, or initialize a new one."""
    # Try to load from memory first
    state = channel_state.get(channel)
    if state is not None:
        return state

    # Try to restore from MongoDB
    state = load_proxy_conversation(channel)
    if state:
        channel_state[channel] = state
        return state

    # Initialize new conversation
    return init_channel_state(channel, requester_user_id, talk_to_myself=talk_to_myself)


def get_stateless_proxy_bot_answer(
        text: str, requester_user_id: str, custom_samples: List[str] = None
) -> str:
    """Programmatic way to get a stateless answer from the proxy bot without hitting Slack or DB."""
    partner_id = requester_user_id
    mode = "mimic"

    style_rules, db_samples, partner_name, personality_details, spc_context = fetch_partner_context(partner_id)
    
    samples_to_use = custom_samples if custom_samples is not None else db_samples
    
    rules = PROXY_BOT_FREE_PROMPT if mode == "free" else PROXY_BOT_MIMIC_PROMPT if mode == "mimic" else PROXY_BOT_PROMPT
    system_prompt = build_system_prompt(partner_name, partner_id, style_rules, samples_to_use, personality_details, spc_context, rules)

    call_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text}
    ]

    try:
        resp = oai.responses.create(model=OPENAI_MODEL,
                                    input=call_messages,
                                    reasoning={
                                        "effort": "medium"
                                    })
        if hasattr(resp, "output_text") and resp.output_text:
            return resp.output_text
        elif hasattr(resp, "output") and isinstance(resp.output, list) and len(resp.output) > 0:
            return getattr(resp.output[0], "content", str(resp.output[0]))
        return "(no response)"
    except Exception as e:
        log.error(f"Error in get_stateless_proxy_bot_answer for {requester_user_id}: {e}")
        return "(error generating response)"


def forward_to_openai_and_reply(
        channel: str, text: str, requester_user_id: Optional[str], event_ts: Optional[float],
        talk_to_myself: bool = False
):
    """Forward the message to OpenAI (as the partner/self) and send reply back to Slack."""
    try:
        # Ignore messages from the bot itself
        if not requester_user_id:
            web.chat_postMessage(
                channel=channel, text="Missing user id for this event."
            )
            return

        state = _get_or_create_channel_state(channel, requester_user_id, talk_to_myself)
        if not state:
            web.chat_postMessage(
                channel=channel, text="No partner configured for you yet."
            )
            return

        convo = state.setdefault("conversation", [])
        user_msg = {"role": "user", "content": text}
        convo.append(user_msg)

        # Build call messages with partner's conversation history
        base = list(state["oa_messages"])

        # Strips metadata to ensure only role and content is being sent to LLM
        clean_convo = []
        for m in convo:
            if m.get("role") and m.get("content"):
                clean_convo.append({"role": m["role"], "content": m["content"]})

        call_messages = base + clean_convo

        messages_payload: Any = call_messages
        parsed: Optional[ProxyResponse] = None
        try:
            resp = oai.responses.parse(
                model=OPENAI_MODEL,
                input=call_messages,
                reasoning={"effort": "medium"},
                text_format=ProxyResponse,
            )
            parsed = resp.output_parsed
        except Exception as e:
            log.error(f"[CLASSIFICATION] responses.parse call failed: {e}")

        if parsed is None:
            # Safe fallback — never ship raw model output to the user.
            log.warning("[CLASSIFICATION] No parsed response; using safe fallback")
            content = "(no response)"
            category = "unknown"
            action = "unknown"
            has_prior_knowledge = False
            confidence = "low"
            extracted_question = None
        else:
            content = parsed.response or "(no response)"
            category = parsed.category
            action = parsed.action
            has_prior_knowledge = parsed.has_prior_knowledge
            confidence = parsed.confidence
            extracted_question = parsed.extracted_question

        if not content or not content.strip():
            content = "(no response)"

        log.info(
            f"[CLASSIFICATION] channel={channel} category={category} action={action} "
            f"has_prior_knowledge={has_prior_knowledge} confidence={confidence} "
            f"extracted_question={extracted_question} "
            f"query={text[:80]}"
        )

        # store unanswered questions so the learning bot can follow up later
        if (
            not has_prior_knowledge
            and category not in ("non_question", "unknown")
            and extracted_question
        ):
            partner_id = resolve_partner(requester_user_id)
            if partner_id:
                try:
                    uq_col = get_unanswered_questions_collection()
                    if uq_col is not None:
                        uq_col.update_one(
                            {"partner_id": partner_id, "question": extracted_question},
                            {
                                "$set": {
                                    "category": category,
                                    "asked_by": requester_user_id,
                                    "channel": channel,
                                    "updated_at": datetime.datetime.utcnow(),
                                },
                                "$setOnInsert": {
                                    "created_at": datetime.datetime.utcnow(),
                                    "answered": False,
                                },
                            },
                            upsert=True,
                        )
                        log.info(
                            f"[GAP] Stored unanswered question for partner={partner_id}: "
                            f"{extracted_question}"
                        )
                except Exception as e:
                    log.warning(f"[GAP] Failed to store unanswered question: {e}")

        # Before appending/sending assistant reply, check if a newer user message arrived
        is_stale = False
        try:
            with _ts_lock:
                latest = _latest_user_ts.get(channel, event_ts or 0.0)
            if event_ts is not None and latest > (event_ts + 1e-9):
                is_stale = True
        except Exception:
            pass

        if is_stale:
            log.info(
                f"[stale-drop] Dropping reply for channel={channel} due to newer user message"
            )
            return

        asst_msg = {
            "role": "assistant",
            "content": content,
            "metadata": {
                "category": category,
                "action": action,
                "has_prior_knowledge": has_prior_knowledge,
                "confidence": confidence,
                "extracted_question": extracted_question,
                "user_query": text,
            },
        }
        convo.append(asst_msg)

        web.chat_postMessage(channel=channel, text=content)

    except Exception as e:
        log.error(f"Error forwarding to OpenAI: {e}")
        try:
            web.chat_postMessage(channel=channel, text="❌ Proxy bot hit an error.")
        except Exception as post_e:
            log.error(f"Also failed to post error message: {post_e}")


def handle_event(event: Dict[str, Any]):
    """Handle a Slack message event in a background thread."""
    # Ignore bot messages and messages without text
    if event.get("subtype") or not event.get("text"):
        return

    if event.get("user") == BOT_USER_ID:
        return

    ev_id = event.get("client_msg_id") or f"{event['ts']}:{event['channel']}"
    if ev_id in seen_events:
        return
    seen_events.add(ev_id)

    channel = event["channel"]
    user_id = event.get("user")
    text = event.get("text", "")
    ts_str = event.get("ts")
    try:
        msg_ts = float(ts_str) if ts_str is not None else 0.0
    except Exception:
        msg_ts = 0.0

    # Update latest user timestamp for this channel
    try:
        with _ts_lock:
            prev = _latest_user_ts.get(channel, 0.0)
            if msg_ts > prev:
                _latest_user_ts[channel] = msg_ts
    except Exception:
        pass

    # Check for clear command
    if text.lower().strip() == "clear":
        clear_proxy_history(channel)
        return

    log.info(f"Proxy received message from {user_id} in {channel}: {text[:120]}")
    forward_to_openai_and_reply(channel, text, user_id, msg_ts)
    