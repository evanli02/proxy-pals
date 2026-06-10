"""
Proxy *sessions*: the mutable, per-conversation state.

Each (viewer, target, conversation) pair gets its own ProxySession with its own
lock. Different conversations never share state, so a target's proxy can be
talked to by any number of viewers at once with no cross-talk and no
contention -- this is what replaces the old module-global ``channel_state`` /
``seen_events`` / ``_latest_user_ts`` dicts that only worked because Slack gave
one DM channel per pair.

``InMemorySessionStore`` is fine for the prototype. A Mongo-backed store
implements the same three methods and swaps in with no engine changes.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass
class ProxySession:
    viewer_id: str
    target_id: str
    conversation_id: str
    # The target's transcript-visibility state captured WHEN THIS CONVERSATION
    # STARTED. Stored per-session so a later toggle can't retroactively expose a
    # chat the viewer held under the expectation of privacy (the consent point).
    target_visibility_on: bool = False
    messages: List[Dict[str, Any]] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def append(self, message: Dict[str, Any]) -> None:
        with self.lock:
            self.messages.append(message)

    def history_snapshot(self) -> List[Dict[str, Any]]:
        """Clean role/content history for the LLM (metadata stripped)."""
        with self.lock:
            return [
                {"role": m["role"], "content": m["content"]}
                for m in self.messages
                if m.get("role") and m.get("content")
            ]


class SessionStore(Protocol):
    def get_or_create(
        self,
        viewer_id: str,
        target_id: str,
        conversation_id: str,
        target_visibility_on: bool,
    ) -> ProxySession:
        ...

    def save(self, session: ProxySession) -> None:
        ...


class InMemorySessionStore:
    def __init__(self):
        self._sessions: Dict[str, ProxySession] = {}
        self._lock = threading.RLock()  # guards the map, not individual sessions

    def get_or_create(
        self,
        viewer_id: str,
        target_id: str,
        conversation_id: str,
        target_visibility_on: bool = False,
    ) -> ProxySession:
        with self._lock:
            session = self._sessions.get(conversation_id)
            if session is None:
                session = ProxySession(
                    viewer_id=viewer_id,
                    target_id=target_id,
                    conversation_id=conversation_id,
                    target_visibility_on=target_visibility_on,
                )
                self._sessions[conversation_id] = session
            return session

    def save(self, session: ProxySession) -> None:
        # No-op for the in-memory store; a Mongo store would upsert here.
        return None
