"""
Proxy *definitions*: the read-only, per-user data that makes a proxy what it is.

This is the heart of the concurrency model. A proxy is **data, not a process**:
once a user finishes training, their definition (style, personality, life
context, sample messages) never changes mid-conversation, so it can be loaded
once, cached, and shared by any number of simultaneous viewers with zero
contention. The mutable, per-conversation part lives in ``sessions.py``.

The data source is injectable. The default reads the same fields the original
``fetch_partner_context`` pulled from Mongo; later you can point ``fetch_record``
at the coalesced training store (question bank + SPC answers) without touching
the engine.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .prompts import build_system_prompt

log = logging.getLogger("core.proxy_definition")

# A function that returns the raw training record for a user_id, or None.
FetchRecord = Callable[[str], Optional[Dict[str, Any]]]
# A function that resolves a user_id to a display name (your users collection).
ResolveName = Callable[[str], str]

_DEFAULT_PERSONALITY = (
    "You have a balanced and adaptable personality. You can be social when the "
    "situation calls for it but also enjoy your own company. You're generally "
    "open to new ideas and experiences, cooperative in your interactions, and "
    "handle stress reasonably well."
)


@dataclass(frozen=True)
class ProxyDefinition:
    """Immutable snapshot of one user's proxy. Safe to share across threads."""

    user_id: str
    display_name: str
    style_rules: Optional[Dict[str, Any]]
    personality: str
    spc_context: Dict[str, str]
    samples: List[str]
    mode: str = "mimic"
    mbti: Optional[str] = None

    def system_prompt(self) -> str:
        return build_system_prompt(
            partner_name=self.display_name,
            style_rules=self.style_rules,
            sample_messages=self.samples,
            personality_details=self.personality,
            spc_context=self.spc_context,
            mode=self.mode,
            mbti=self.mbti,
        )


def _extract_user_messages(messages: List[Dict[str, Any]]) -> List[str]:
    """Recent user/assistant turns flattened to strings (ported behavior)."""
    return [
        ("Question: " if m.get("role") == "assistant" else "user: ") + m.get("content", "")
        for m in messages
        if m.get("content") and m.get("role") in ("user", "assistant")
    ]


def _default_fetch_record(user_id: str) -> Optional[Dict[str, Any]]:
    """Mongo-backed default: newest conversation doc for the user.

    Imported lazily so the core stays importable without pymongo/credentials.
    """
    from commons.db import get_conversations_collection

    col = get_conversations_collection()
    if col is None:
        return None
    doc = next(iter(col.find({"user_id": user_id}).sort("updated_at", -1).limit(1)), None)
    return doc


def record_to_definition(
    user_id: str,
    record: Optional[Dict[str, Any]],
    display_name: str,
    mode: str = "mimic",
) -> ProxyDefinition:
    """Map a raw training record into an immutable ProxyDefinition."""
    record = record or {}
    spc_raw = record.get("spc_raw", {})
    spc_context = spc_raw.get("context", {}) if isinstance(spc_raw, dict) else {}
    return ProxyDefinition(
        user_id=user_id,
        display_name=display_name,
        style_rules=record.get("style_rules"),
        personality=record.get("personality", _DEFAULT_PERSONALITY),
        spc_context=spc_context or {},
        samples=_extract_user_messages(record.get("messages", [])),
        mode=mode,
        mbti=record.get("mbti") or None,
    )


class ProxyDefinitionCache:
    """Thread-safe cache of immutable definitions keyed by user_id.

    Because definitions are frozen, many threads can read the same one at once.
    Call ``invalidate(user_id)`` after a user re-trains or edits their proxy.

    (A bounded LRU / TTL can be layered on later; for the prototype an explicit
    invalidate-on-write is simpler and correct.)
    """

    def __init__(
        self,
        fetch_record: FetchRecord = _default_fetch_record,
        resolve_name: Optional[ResolveName] = None,
    ):
        self._fetch_record = fetch_record
        self._resolve_name = resolve_name or (lambda uid: uid)
        self._store: Dict[str, ProxyDefinition] = {}
        self._lock = threading.RLock()

    def get(self, user_id: str, mode: str = "mimic") -> ProxyDefinition:
        with self._lock:
            cached = self._store.get(user_id)
            if cached is not None and cached.mode == mode:
                return cached
        # Build outside the lock; building is read-only and may do I/O.
        record = self._fetch_record(user_id)
        definition = record_to_definition(
            user_id, record, self._resolve_name(user_id), mode=mode
        )
        with self._lock:
            self._store[user_id] = definition
        return definition

    def invalidate(self, user_id: str) -> None:
        with self._lock:
            self._store.pop(user_id, None)
