
#!/usr/bin/env python3
import os
import re
import json
import logging
from collections import defaultdict
from typing import Dict, List, Any

from commons.db import get_mongo_client, get_conversations_collection
from dotenv import load_dotenv
from openai import OpenAI

from prompts.schema_text import SCHEMA_INSTRUCTIONS


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("conversation_analyzer")

# Load environment variables
load_dotenv()

RE_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

def _strip_code_fences(s: str) -> str:
    """Remove common markdown code fences, just in case."""
    return RE_JSON_FENCE.sub("", s).strip()

def _slice_outer_braces(s: str) -> str:
    """Best-effort extraction of the first {...} block from a string."""
    if not s:
        return s
    first = s.find("{")
    last = s.rfind("}")
    if first != -1 and last != -1 and last > first:
        return s[first:last+1]
    return s

def _cap_messages(messages: List[str], max_messages: int, max_chars: int) -> str:
    """
    Join the last N messages but also cap total characters to prevent prompt bloat.
    Preference is given to the most recent messages.
    """
    msgs = messages[-max_messages:]
    joined = "\n".join(msgs)
    if len(joined) <= max_chars:
        return joined
    # Trim from the start (keep most recent content)
    return joined[-max_chars:]

class ConversationAnalyzer:
    def __init__(self):
        """Initialize MongoDB connection and analyzer"""
        self.mongo_client = None
        self.conversations_collection = None
        self.oai = None
        self._connect_to_mongodb()

        # Configurable caps
        self.MAX_MESSAGES = int(os.environ.get("ANALYSIS_MAX_MESSAGES", "120"))
        self.MAX_CHARS = int(os.environ.get("ANALYSIS_MAX_CHARS", "12000"))
        self.MAX_TOKENS = int(os.environ.get("ANALYSIS_MAX_TOKENS", "2000"))  # used for output

    def _connect_to_mongodb(self):
        """Connect to MongoDB singleton and initialize OpenAI client"""
        try:
            self.mongo_client = get_mongo_client()
            self.conversations_collection = get_conversations_collection()
            if self.conversations_collection is not None:
                log.info("Connected to MongoDB successfully (singleton)")
            else:
                log.error("MongoDB connection not available")
        except Exception as e:
            log.error(f"Failed to connect to MongoDB: {e}")
            self.mongo_client = None

        try:
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                log.error("OPENAI_API_KEY not set in environment variables")
            else:
                self.oai = OpenAI(api_key=api_key)
        except Exception as e:
            log.error(f"Failed to initialize OpenAI client: {e}")

    def extract_user_messages(self, messages: List[Dict]) -> List[str]:
        """Extract only user messages from conversation"""
        user_messages = []
        for message in messages:
            if message.get('role') == 'user' and message.get('content'):
                user_messages.append(message['content'])
        return user_messages

    def analyze_user_style(self, user_id: str, user_name: str, messages: List[str]) -> Dict[str, Any]:
        """Use OpenAI to return analysis JSON with the required schema"""
        log.info(f"Analyzing style via OpenAI for user {user_name} ({user_id})")

        fallback: Dict[str, Any] = {
            "summary": "",
            "formality": "unknown",
            "cadence": {"avg_sentence_length": "unknown", "rhythm_notes": "unknown"},
            "punctuation": {"traits": []},
            "emoji_and_markers": {"emoji_frequency": "0%", "markers": []},
            "lexicon": {"register": "neutral", "favorite_words": [], "hedges": [], "intensifiers": []},
            "style_rules_do": [],
            "style_rules_dont": [],
            "safety_note": "Analysis generated with limited context."
        }

        if not messages:
            return fallback
        if self.oai is None:
            log.error("OpenAI client not initialized; returning fallback analysis")
            return fallback

        # Cap prompt size
        joined_messages = _cap_messages(messages, self.MAX_MESSAGES, self.MAX_CHARS)

        try:
            resp = self.oai.chat.completions.create(
                model=os.environ.get("ANALYSIS_MODEL", "gpt-5-mini"),
                messages=[
                    {"role": "system", "content": "You are an expert conversation analyst. You must respond with ONLY valid JSON. Never use markdown formatting or code blocks."},
                    {"role": "user", "content": (
                        f"Analyze the user's writing style from the following messages.\n\n"
                        f"{SCHEMA_INSTRUCTIONS}\n\n"
                        f"User messages:\n{joined_messages}\n\n"
                        f"Remember: Return ONLY valid JSON starting with {{ and ending with }}. No markdown, no code blocks, no additional text."
                    )},
                ],
            )

            content = resp.choices[0].message.content if resp and resp.choices else None
            log.warning(f"OpenAI content (first 500 chars): {str(content)[:500]}")
            if not content or not isinstance(content, str):
                log.warning("OpenAI returned empty content; using fallback")
                return fallback

            # Cleanup & parse
            cleaned = _strip_code_fences(content.strip())
            # Prefer exact outer braces if any stray text is present
            cleaned = _slice_outer_braces(cleaned)

            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError as e:
                log.warning(f"Strict JSON parse failed: {e}. Attempting recovery.")

            if not isinstance(parsed, dict):
                log.warning("Failed to parse/recover JSON. Using fallback.")
                return fallback

            # Ensure required keys exist
            required_keys = [
                "summary", "formality", "cadence", "punctuation", "emoji_and_markers",
                "lexicon", "style_rules_do", "style_rules_dont", "safety_note"
            ]
            for k in required_keys:
                if k not in parsed:
                    parsed[k] = fallback[k]

            return parsed

        except Exception as e:
            log.error(f"OpenAI analysis failed: {e}")
            return fallback

    def process_all_conversations(self):
        """Analyze all conversations and save results"""
        if self.conversations_collection is None:
            log.error("MongoDB connection not available")
            return

        log.info("Starting conversation analysis.")

        # Get all conversations
        conversations = list(self.conversations_collection.find())
        log.info(f"Found {len(conversations)} conversations to analyze")

        # Group messages by user
        user_messages: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        for conversation in conversations:
            user_id = conversation.get('user_id')
            user_name = conversation.get('user_name', 'Unknown')
            messages = conversation.get('messages', [])

            if user_id and messages:
                user_messages[user_id].append({
                    'user_name': user_name,
                    'messages': self.extract_user_messages(messages)
                })

        # Perform analysis for each user
        for user_id, user_data in user_messages.items():
            try:
                # Combine all messages for this user (across channels)
                all_messages: List[str] = []
                user_name = user_data[0]['user_name'] if user_data else 'Unknown'

                for data in user_data:
                    all_messages.extend(data['messages'])

                if not all_messages:
                    log.warning(f"No user messages found for user {user_id}")
                    continue

                # Perform style analysis
                analysis = self.analyze_user_style(user_id, user_name, all_messages)

                # Save entire analysis JSON under style_rules
                style_rules_doc = {
                    "style_rules": analysis,
                    "user_id": user_id,
                    "user_name": user_name,
                    "analysis_metadata": {
                        "total_messages_analyzed": len(all_messages),
                        "analysis_date": __import__('time').time(),
                        "analysis_version": "1.1-refactor"
                    }
                }

                # Update all conversation docs for this user (may have multiple channels)
                self.conversations_collection.update_many(
                    {"user_id": user_id},
                    {"$set": style_rules_doc},
                    upsert=False
                )

                log.info(f"Analysis completed for user {user_name} ({user_id}) - {len(all_messages)} messages")

            except Exception as e:
                log.error(f"Error analyzing user {user_id}: {e}")
                continue

        log.info("Conversation analysis completed!")
