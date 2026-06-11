"""
Mongo-backed persistence for the core engines (write-through caches).

Design: the in-memory object (ProxySession / InterviewState) remains the live,
lock-protected source of truth *within the process*; every ``save()`` upserts a
snapshot to Mongo so state survives restarts and can be loaded cold. This keeps
the engine code identical whether you run the in-memory stores or these.

New collections (following the existing get_*_collection conventions):
  - proxy_sessions   {conversation_id, viewer_id, target_id,
                      target_visibility_on, messages, updated_at}
  - interviews       {user_id, asked_ids, follow_up_ids, previous_question,
                      previous_question_id, messages, profile_ready, updated_at}

Caveat (documented, acceptable for the prototype): write-through caching is
correct for a SINGLE web process. If you scale to multiple workers, two
processes could hold divergent copies of the same conversation; at that point
switch save() to atomic $push of new messages instead of snapshot upserts.
Run the prototype with one worker (uvicorn default).
"""
from __future__ import annotations

import datetime
import logging
import threading
from typing import Any, Dict, List, Optional

from .schemas import UnansweredQuestion
from .sessions import ProxySession
from .interview import InterviewState

log = logging.getLogger("core.mongo_stores")


# --- collection accessors (same pattern as commons/db.py) -------------------

def get_proxy_sessions_collection():
    from commons.db import get_db

    database = get_db()
    if database is None:
        return None
    try:
        return database.proxy_sessions
    except Exception as e:
        log.error(f"Failed to access proxy_sessions collection: {e}")
        return None


def get_interviews_collection():
    from commons.db import get_db

    database = get_db()
    if database is None:
        return None
    try:
        return database.interviews
    except Exception as e:
        log.error(f"Failed to access interviews collection: {e}")
        return None


# --- proxy sessions ----------------------------------------------------------

class MongoSessionStore:
    """SessionStore backed by Mongo with an in-process write-through cache."""

    def __init__(self, collection=None):
        self._collection = collection  # injectable for tests
        self._cache: Dict[str, ProxySession] = {}
        self._lock = threading.RLock()

    def _col(self):
        return self._collection if self._collection is not None else get_proxy_sessions_collection()

    def get_or_create(
        self,
        viewer_id: str,
        target_id: str,
        conversation_id: str,
        target_visibility_on: bool = False,
    ) -> ProxySession:
        with self._lock:
            cached = self._cache.get(conversation_id)
            if cached is not None:
                return cached

        session = None
        col = self._col()
        if col is not None:
            try:
                doc = col.find_one({"conversation_id": conversation_id})
                if doc:
                    session = ProxySession(
                        viewer_id=doc["viewer_id"],
                        target_id=doc["target_id"],
                        conversation_id=conversation_id,
                        # visibility snapshot is whatever it was AT CREATION;
                        # never refreshed from the live toggle (consent rule).
                        target_visibility_on=bool(doc.get("target_visibility_on", False)),
                        messages=list(doc.get("messages", [])),
                    )
                    log.info(f"Loaded proxy session {conversation_id} from MongoDB")
            except Exception as e:
                log.error(f"Failed loading session {conversation_id}: {e}")

        if session is None:
            session = ProxySession(
                viewer_id=viewer_id,
                target_id=target_id,
                conversation_id=conversation_id,
                target_visibility_on=target_visibility_on,
            )

        with self._lock:
            # another thread may have raced us; keep the first one
            existing = self._cache.get(conversation_id)
            if existing is not None:
                return existing
            self._cache[conversation_id] = session
            return session

    def save(self, session: ProxySession) -> None:
        col = self._col()
        if col is None:
            return
        try:
            with session.lock:
                snapshot = {
                    "viewer_id": session.viewer_id,
                    "target_id": session.target_id,
                    "target_visibility_on": session.target_visibility_on,
                    "messages": list(session.messages),
                    "updated_at": datetime.datetime.utcnow(),
                }
            col.update_one(
                {"conversation_id": session.conversation_id},
                {"$set": snapshot,
                 "$setOnInsert": {"created_at": datetime.datetime.utcnow()}},
                upsert=True,
            )
        except Exception as e:
            log.error(f"Failed saving session {session.conversation_id}: {e}")


# --- interviews --------------------------------------------------------------

class MongoInterviewStore:
    """Interview store backed by Mongo with an in-process write-through cache."""

    def __init__(self, collection=None):
        self._collection = collection
        self._cache: Dict[str, InterviewState] = {}
        self._lock = threading.RLock()

    def _col(self):
        return self._collection if self._collection is not None else get_interviews_collection()

    def get_or_create(self, user_id: str) -> InterviewState:
        with self._lock:
            cached = self._cache.get(user_id)
            if cached is not None:
                return cached

        state = None
        col = self._col()
        if col is not None:
            try:
                doc = col.find_one({"user_id": user_id})
                if doc:
                    state = InterviewState(
                        user_id=user_id,
                        asked_ids=list(doc.get("asked_ids", [])),
                        follow_up_ids=list(doc.get("follow_up_ids", [])),
                        previous_question=doc.get("previous_question", "") or "",
                        previous_question_id=doc.get("previous_question_id", "") or "",
                        pending_structured_id=doc.get("pending_structured_id", "") or "",
                        structured_answers=dict(doc.get("structured_answers", {})),
                        messages=list(doc.get("messages", [])),
                    )
                    log.info(f"Loaded interview state for {user_id} from MongoDB")
            except Exception as e:
                log.error(f"Failed loading interview for {user_id}: {e}")

        if state is None:
            state = InterviewState(user_id=user_id)

        with self._lock:
            existing = self._cache.get(user_id)
            if existing is not None:
                return existing
            self._cache[user_id] = state
            return state

    def reset(self, user_id: str) -> None:
        with self._lock:
            self._cache.pop(user_id, None)
        col = self._col()
        if col is not None:
            try:
                col.delete_one({"user_id": user_id})
            except Exception as e:
                log.error(f"Failed resetting interview for {user_id}: {e}")

    def save(self, state: InterviewState, profile_ready: bool = False) -> None:
        col = self._col()
        if col is None:
            return
        try:
            with state.lock:
                snapshot = {
                    "asked_ids": list(state.asked_ids),
                    "follow_up_ids": list(state.follow_up_ids),
                    "previous_question": state.previous_question,
                    "previous_question_id": state.previous_question_id,
                    "pending_structured_id": state.pending_structured_id,
                    "structured_answers": dict(state.structured_answers),
                    "messages": list(state.messages),
                    "updated_at": datetime.datetime.utcnow(),
                }
                if profile_ready:
                    snapshot["profile_ready"] = True
            col.update_one(
                {"user_id": state.user_id},
                {"$set": snapshot,
                 "$setOnInsert": {"created_at": datetime.datetime.utcnow()}},
                upsert=True,
            )
        except Exception as e:
            log.error(f"Failed saving interview for {state.user_id}: {e}")


# --- knowledge: the QA pairs your standin CAN answer -------------------------

class MongoKnowledgeStore:
    """List/edit/delete a user's qa_pairs. Edits rebuild qa_text and re-embed
    through the existing rag.store path, so RAG search reflects them
    immediately. Deletes remove the fact outright."""

    def _col(self):
        from commons.db import get_db

        db = get_db()
        return None if db is None else db.qa_pairs

    def list(self, user_id: str, limit: int = 200):
        col = self._col()
        if col is None:
            return []
        out = []
        for doc in col.find(
            {"user_id": user_id},
            {"qa_id": 1, "question_text": 1, "answer_text": 1, "created_at": 1},
        ).sort("created_at", -1).limit(limit):
            out.append({
                "id": doc.get("qa_id") or str(doc["_id"]),
                "question": doc.get("question_text", ""),
                "answer": doc.get("answer_text", ""),
                "created_at": str(doc.get("created_at", "")),
            })
        return out

    def update(self, user_id: str, qa_id: str, new_answer: str) -> bool:
        col = self._col()
        if col is None:
            return False
        doc = col.find_one({"_id": qa_id, "user_id": user_id})
        if not doc:
            return False
        q = doc.get("question_text", "")
        doc["answer_text"] = new_answer
        doc["qa_text"] = f"Q: {q}\nA: {new_answer}"
        try:
            from proxy_bot.rag.store import upsert_qa_items

            upsert_qa_items([doc])  # re-embeds the new qa_text, upserts by _id
            return True
        except Exception as e:
            log.error(f"Failed re-embedding edited QA {qa_id}: {e}")
            return False

    def delete(self, user_id: str, qa_id: str) -> bool:
        col = self._col()
        if col is None:
            return False
        return col.delete_one({"_id": qa_id, "user_id": user_id}).deleted_count > 0


# --- review loop: answer the questions your proxy couldn't ------------------

class MongoReviewStore:
    """Pending unanswered_questions -> user answers them -> each answer becomes
    a qa_pair (embedded + searchable) and the gap is marked answered. This is
    the web edition of the Slack app's closed-loop review feature."""

    def _col(self):
        from commons.db import get_unanswered_questions_collection

        return get_unanswered_questions_collection()

    def pending(self, user_id: str, limit: int = 50):
        col = self._col()
        if col is None:
            return []
        out = []
        for doc in col.find({"user_id": user_id, "status": "pending"}).sort(
            "created_at", -1
        ).limit(limit):
            out.append({
                "id": str(doc["_id"]),
                "question": doc.get("question", ""),
                "category": doc.get("category", ""),
                "created_at": str(doc.get("created_at", "")),
            })
        return out

    def answer(self, user_id: str, item_id: str, answer_text: str) -> bool:
        from bson import ObjectId
        from core.training_compiler import decomposed_pairs_to_qa_items

        col = self._col()
        if col is None:
            return False
        try:
            doc = col.find_one({"_id": ObjectId(item_id), "user_id": user_id,
                                "status": "pending"})
        except Exception:
            return False
        if not doc:
            return False
        items = decomposed_pairs_to_qa_items(
            user_id,
            [{"question": doc.get("question", ""), "answer": answer_text}],
            datetime.datetime.utcnow().timestamp(),
        )
        # re-key so review answers don't collide with anything-else ids
        for i, it in enumerate(items):
            it["q_msg_id"] = f"rv_q_{item_id}_{i}"
            it["a_msg_id"] = f"rv_a_{item_id}_{i}"
        try:
            from proxy_bot.rag.store import upsert_qa_items

            upsert_qa_items(items)
        except Exception as e:
            log.error(f"Failed embedding review answer: {e}")
            return False
        col.update_one({"_id": doc["_id"]},
                       {"$set": {"status": "answered",
                                 "answer": answer_text,
                                 "answered_at": datetime.datetime.utcnow()}})
        return True


# --- unanswered questions (gap queue) ---------------------------------------

def persist_unanswered_question(gap: UnansweredQuestion, collection=None) -> bool:
    """Write a gap to the existing unanswered_questions collection.

    The engine returns gaps as data; the web layer calls this. Shape mirrors the
    old proxy writes, extended with web-era fields (asked_by, conversation_id).
    """
    col = collection
    if col is None:
        from commons.db import get_unanswered_questions_collection

        col = get_unanswered_questions_collection()
    if col is None:
        return False
    try:
        col.insert_one({
            "user_id": gap.target_id,           # whose proxy lacked the answer
            "asked_by": gap.asked_by,
            "conversation_id": gap.conversation_id,
            "question": gap.question,
            "category": gap.category,
            "status": "pending",
            "created_at": gap.created_at,
        })
        return True
    except Exception as e:
        log.error(f"Failed persisting unanswered question: {e}")
        return False
