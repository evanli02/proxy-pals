"""
The social layer: likes, mutual connections, and direct messages.

Rules encoded here:
  - a "like" is one-directional and anonymous (the receiver sees only the
    sender's pseudonym/avatar until things become mutual)
  - mutual likes create a connection; connections unlock full profiles and DMs
  - DMs are allowed ONLY between connected users

InMemorySocialStore for tests; MongoSocialStore for production
(collections: likes, connections, dms).
"""
from __future__ import annotations

import datetime
import threading
from typing import Any, Dict, List, Optional, Tuple


def _pair_key(a: str, b: str) -> str:
    return "::".join(sorted([a, b]))


class InMemorySocialStore:
    def __init__(self):
        self._likes: Dict[Tuple[str, str], Any] = {}   # (from, to) -> ts
        self._connections: Dict[str, Any] = {}          # pair_key -> ts
        self._dms: Dict[str, List[Dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def like(self, from_id: str, to_id: str) -> bool:
        """Record a like; returns True if it completed a mutual connection."""
        if from_id == to_id:
            raise ValueError("cannot like yourself")
        with self._lock:
            self._likes[(from_id, to_id)] = datetime.datetime.utcnow()
            if (to_id, from_id) in self._likes:
                self._connections.setdefault(_pair_key(from_id, to_id),
                                             datetime.datetime.utcnow())
                return True
            return False

    def liked(self, from_id: str, to_id: str) -> bool:
        with self._lock:
            return (from_id, to_id) in self._likes

    def connected(self, a: str, b: str) -> bool:
        with self._lock:
            return _pair_key(a, b) in self._connections

    def incoming_likes(self, user_id: str) -> List[str]:
        """Sender ids of pending likes (not yet mutual)."""
        with self._lock:
            return [f for (f, t) in self._likes
                    if t == user_id and not self.connected(f, t)]

    def connections_of(self, user_id: str) -> List[str]:
        with self._lock:
            out = []
            for key in self._connections:
                a, b = key.split("::")
                if user_id in (a, b):
                    out.append(b if a == user_id else a)
            return out

    def send_dm(self, from_id: str, to_id: str, text: str) -> Dict[str, Any]:
        msg = {"from": from_id, "to": to_id, "text": text,
               "at": datetime.datetime.utcnow().isoformat()}
        with self._lock:
            self._dms.setdefault(_pair_key(from_id, to_id), []).append(msg)
        return msg

    def get_dms(self, a: str, b: str, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._dms.get(_pair_key(a, b), []))[-limit:]


class MongoSocialStore:
    """Same surface on Mongo. Lazy collections: likes, connections, dms."""

    def _db(self):
        from commons.db import get_db
        return get_db()

    def like(self, from_id: str, to_id: str) -> bool:
        if from_id == to_id:
            raise ValueError("cannot like yourself")
        db = self._db()
        if db is None:
            raise RuntimeError("database unavailable")
        now = datetime.datetime.utcnow()
        db.likes.update_one({"from_id": from_id, "to_id": to_id},
                            {"$setOnInsert": {"created_at": now}}, upsert=True)
        if db.likes.find_one({"from_id": to_id, "to_id": from_id}):
            db.connections.update_one({"pair": _pair_key(from_id, to_id)},
                                      {"$setOnInsert": {"created_at": now}},
                                      upsert=True)
            return True
        return False

    def liked(self, from_id: str, to_id: str) -> bool:
        db = self._db()
        return bool(db and db.likes.find_one({"from_id": from_id, "to_id": to_id}))

    def connected(self, a: str, b: str) -> bool:
        db = self._db()
        return bool(db and db.connections.find_one({"pair": _pair_key(a, b)}))

    def incoming_likes(self, user_id: str) -> List[str]:
        db = self._db()
        if db is None:
            return []
        out = []
        for doc in db.likes.find({"to_id": user_id}).sort("created_at", -1):
            if not self.connected(doc["from_id"], user_id):
                out.append(doc["from_id"])
        return out

    def connections_of(self, user_id: str) -> List[str]:
        db = self._db()
        if db is None:
            return []
        out = []
        for doc in db.connections.find({"pair": {"$regex": user_id}}):
            a, b = doc["pair"].split("::")
            if user_id in (a, b):
                out.append(b if a == user_id else a)
        return out

    def send_dm(self, from_id: str, to_id: str, text: str) -> Dict[str, Any]:
        db = self._db()
        if db is None:
            raise RuntimeError("database unavailable")
        msg = {"pair": _pair_key(from_id, to_id), "from": from_id, "to": to_id,
               "text": text, "at": datetime.datetime.utcnow()}
        db.dms.insert_one(dict(msg))
        msg["at"] = msg["at"].isoformat()
        msg.pop("pair", None)
        msg.pop("_id", None)
        return msg

    def get_dms(self, a: str, b: str, limit: int = 200) -> List[Dict[str, Any]]:
        db = self._db()
        if db is None:
            return []
        out = []
        for doc in db.dms.find({"pair": _pair_key(a, b)}).sort("at", 1).limit(limit):
            out.append({"from": doc["from"], "to": doc["to"], "text": doc["text"],
                        "at": str(doc["at"])})
        return out
