"""
The users layer: accounts, tokens, profile fields, and photos.

Two interchangeable implementations of the same surface:
  - InMemoryUserStore: offline tests
  - MongoUserStore: collections `users`, `auth_tokens`, `photos`

User document shape:
  {
    user_id, email, password_hash,
    name, age, bio,
    photos: [photo_id, ...]            # ordered, max 6
    transcript_visibility: bool,       # the V/T toggle
    profile_live: bool,                # flips when interview profile_ready
    created_at, updated_at
  }

Photos are stored as raw bytes in their own collection/dict (well under the
16MB BSON cap given the 5MB per-photo limit) and served by GET /api/photos/{id}.
For the prototype this avoids standing up S3/Cloudinary; the store interface
keeps that swap contained later.
"""
from __future__ import annotations

import datetime
import logging
import random
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("webapp.users")

_PSEUDO_ADJ = ["Cobalt", "Mossy", "Amber", "Velvet", "Static", "Lunar", "Peppered",
               "Cosmic", "Maple", "Foggy", "Neon", "Quiet", "Wobbly", "Golden"]
_PSEUDO_NOUN = ["Fox", "Otter", "Comet", "Cactus", "Sparrow", "Noodle", "Glacier",
                "Puffin", "Meteor", "Fern", "Walrus", "Lantern", "Mango", "Yeti"]

AVATAR_BG = ["#DFE9E4", "#F4E6D5", "#E4E0F0", "#F0E4E4", "#E0EDF0", "#EFEBD8",
             "#EAE0EC", "#DCEBE0", "#F1E3D3", "#E3E7F0"]
AVATAR_BODY = ["#2F5D50", "#C77E3C", "#6B5B9E", "#A3524B", "#3F7D8C", "#8C7B3F",
               "#B85C79", "#4A6FA5", "#5E8C61", "#8A6552"]
AVATAR_SHAPE = ["blob", "round", "square", "bean", "egg"]
AVATAR_EYES = ["dot", "happy", "star", "sleepy", "wink", "big", "side", "shades"]
AVATAR_MOUTH = ["smile", "open", "flat", "cat", "grin", "tongue", "smirk", "ooo"]
AVATAR_ACC = ["none", "sprout", "halo", "antenna", "bow", "crown", "flower",
              "headphones", "horns", "beanie"]
AVATAR_PATTERN = ["none", "none", "spots", "stripes", "belly"]  # none weighted


def random_pseudonym() -> str:
    return f"{random.choice(_PSEUDO_ADJ)} {random.choice(_PSEUDO_NOUN)}"


def random_avatar() -> Dict[str, str]:
    return {
        "bg": random.choice(AVATAR_BG),
        "body": random.choice(AVATAR_BODY),
        "shape": random.choice(AVATAR_SHAPE),
        "eyes": random.choice(AVATAR_EYES),
        "mouth": random.choice(AVATAR_MOUTH),
        "acc": random.choice(AVATAR_ACC),
        "pattern": random.choice(AVATAR_PATTERN),
        "blush": random.choice(["off", "off", "on"]),
    }

MAX_PHOTOS = 6
MAX_PHOTO_BYTES = 5 * 1024 * 1024
ALLOWED_PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}

PROFILE_EDITABLE_FIELDS = {"name", "age", "bio", "city", "transcript_visibility",
                           "proxy_mode", "pseudonym", "gender", "avatar"}
PROXY_MODES = ("strict", "mimic", "free")


class DuplicateEmailError(Exception):
    pass


class PhotoLimitError(Exception):
    pass


def _new_user_doc(email: str, password_hash: str, name: str, age: int) -> Dict[str, Any]:
    now = datetime.datetime.utcnow()
    return {
        "user_id": f"u_{uuid.uuid4().hex}",
        "email": email.strip().lower(),
        "password_hash": password_hash,
        "name": name,
        "age": age,
        "bio": "",
        "city": "",
        "gender": "",
        "pseudonym": random_pseudonym(),
        "avatar": random_avatar(),
        "proxy_mode": "mimic",
        "photos": [],
        "transcript_visibility": False,
        "profile_live": False,
        "created_at": now,
        "updated_at": now,
    }


def anon_profile(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The anonymous face of a profile: pseudonym + avatar, nothing more.
    This is all a stranger sees until a mutual connection exists."""
    return {
        "user_id": doc["user_id"],
        "pseudonym": doc.get("pseudonym", "Anonymous"),
        "avatar": doc.get("avatar", {}),
        "profile_live": bool(doc.get("profile_live", False)),
        "transcript_visibility": bool(doc.get("transcript_visibility", False)),
        "anonymous": True,
    }


def public_profile(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The FULL profile -- only for the owner and mutual connections."""
    return {
        "user_id": doc["user_id"],
        "name": doc["name"],
        "age": doc["age"],
        "bio": doc.get("bio", ""),
        "city": doc.get("city", ""),
        "gender": doc.get("gender", ""),
        "pseudonym": doc.get("pseudonym", ""),
        "avatar": doc.get("avatar", {}),
        "proxy_mode": doc.get("proxy_mode", "mimic"),
        "anonymous": False,
        "photos": list(doc.get("photos", [])),
        "transcript_visibility": bool(doc.get("transcript_visibility", False)),
        "profile_live": bool(doc.get("profile_live", False)),
    }


def own_profile(doc: Dict[str, Any]) -> Dict[str, Any]:
    """What the account owner sees about themselves."""
    p = public_profile(doc)
    p["email"] = doc["email"]
    return p


# --- in-memory ----------------------------------------------------------------

class InMemoryUserStore:
    def __init__(self):
        self._users: Dict[str, Dict[str, Any]] = {}
        self._by_email: Dict[str, str] = {}
        self._tokens: Dict[str, str] = {}            # token_hash -> user_id
        self._photos: Dict[str, Tuple[bytes, str]] = {}  # photo_id -> (bytes, type)
        self._lock = threading.RLock()

    # accounts
    def create_user(self, email: str, password_hash: str, name: str, age: int) -> Dict[str, Any]:
        email_n = email.strip().lower()
        with self._lock:
            if email_n in self._by_email:
                raise DuplicateEmailError(email_n)
            doc = _new_user_doc(email_n, password_hash, name, age)
            self._users[doc["user_id"]] = doc
            self._by_email[email_n] = doc["user_id"]
            return doc

    def get_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            uid = self._by_email.get(email.strip().lower())
            return dict(self._users[uid]) if uid else None

    def get_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            doc = self._users.get(user_id)
            return dict(doc) if doc else None

    # tokens
    def save_token(self, token_hash: str, user_id: str) -> None:
        with self._lock:
            self._tokens[token_hash] = user_id

    def user_id_for_token(self, token_hash: str) -> Optional[str]:
        with self._lock:
            return self._tokens.get(token_hash)

    # profile
    def update_profile(self, user_id: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        clean = {k: v for k, v in fields.items() if k in PROFILE_EDITABLE_FIELDS and v is not None}
        with self._lock:
            doc = self._users.get(user_id)
            if doc is None:
                return None
            doc.update(clean)
            doc["updated_at"] = datetime.datetime.utcnow()
            return dict(doc)

    def set_profile_live(self, user_id: str) -> None:
        with self._lock:
            doc = self._users.get(user_id)
            if doc is not None:
                doc["profile_live"] = True
                doc["updated_at"] = datetime.datetime.utcnow()

    def get_visibility(self, user_id: str) -> bool:
        with self._lock:
            doc = self._users.get(user_id)
            return bool(doc and doc.get("transcript_visibility", False))

    # photos
    def add_photo(self, user_id: str, content: bytes, content_type: str) -> str:
        with self._lock:
            doc = self._users.get(user_id)
            if doc is None:
                raise KeyError(user_id)
            if len(doc["photos"]) >= MAX_PHOTOS:
                raise PhotoLimitError()
            photo_id = f"ph_{uuid.uuid4().hex}"
            self._photos[photo_id] = (content, content_type)
            doc["photos"].append(photo_id)
            doc["updated_at"] = datetime.datetime.utcnow()
            return photo_id

    def delete_photo(self, user_id: str, photo_id: str) -> bool:
        with self._lock:
            doc = self._users.get(user_id)
            if doc is None or photo_id not in doc["photos"]:
                return False
            doc["photos"].remove(photo_id)
            self._photos.pop(photo_id, None)
            doc["updated_at"] = datetime.datetime.utcnow()
            return True

    def get_photo(self, photo_id: str) -> Optional[Tuple[bytes, str]]:
        with self._lock:
            return self._photos.get(photo_id)

    # lo-fi discovery placeholder (real browse/filters come in Phase 3)
    def list_live_profiles(self, exclude_user_id: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            out = [
                anon_profile(d) for d in self._users.values()
                if d.get("profile_live") and d["user_id"] != exclude_user_id
            ]
            return out[:limit]


# --- mongo ----------------------------------------------------------------------

class MongoUserStore:
    """Same surface, backed by Mongo. Collections created lazily on first use."""

    def _db(self):
        from commons.db import get_db
        return get_db()

    def _users(self):
        db = self._db()
        return None if db is None else db.users

    def _tokens(self):
        db = self._db()
        return None if db is None else db.auth_tokens

    def _photos(self):
        db = self._db()
        return None if db is None else db.photos

    # accounts
    def create_user(self, email: str, password_hash: str, name: str, age: int) -> Dict[str, Any]:
        col = self._users()
        if col is None:
            raise RuntimeError("database unavailable")
        email_n = email.strip().lower()
        if col.find_one({"email": email_n}):
            raise DuplicateEmailError(email_n)
        doc = _new_user_doc(email_n, password_hash, name, age)
        col.insert_one(dict(doc))
        doc.pop("_id", None)
        return doc

    def get_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        col = self._users()
        return None if col is None else col.find_one({"email": email.strip().lower()})

    def get_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        col = self._users()
        return None if col is None else col.find_one({"user_id": user_id})

    # tokens
    def save_token(self, token_hash: str, user_id: str) -> None:
        col = self._tokens()
        if col is not None:
            col.insert_one({
                "token_hash": token_hash,
                "user_id": user_id,
                "created_at": datetime.datetime.utcnow(),
            })

    def user_id_for_token(self, token_hash: str) -> Optional[str]:
        col = self._tokens()
        if col is None:
            return None
        doc = col.find_one({"token_hash": token_hash})
        return doc["user_id"] if doc else None

    # profile
    def update_profile(self, user_id: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        clean = {k: v for k, v in fields.items() if k in PROFILE_EDITABLE_FIELDS and v is not None}
        col = self._users()
        if col is None:
            return None
        clean["updated_at"] = datetime.datetime.utcnow()
        col.update_one({"user_id": user_id}, {"$set": clean})
        return col.find_one({"user_id": user_id})

    def set_profile_live(self, user_id: str) -> None:
        col = self._users()
        if col is not None:
            col.update_one(
                {"user_id": user_id},
                {"$set": {"profile_live": True,
                          "updated_at": datetime.datetime.utcnow()}},
            )

    def get_visibility(self, user_id: str) -> bool:
        col = self._users()
        if col is None:
            return False
        doc = col.find_one({"user_id": user_id}, {"transcript_visibility": 1})
        return bool(doc and doc.get("transcript_visibility", False))

    # photos
    def add_photo(self, user_id: str, content: bytes, content_type: str) -> str:
        users = self._users()
        photos = self._photos()
        if users is None or photos is None:
            raise RuntimeError("database unavailable")
        doc = users.find_one({"user_id": user_id}, {"photos": 1})
        if doc is None:
            raise KeyError(user_id)
        if len(doc.get("photos", [])) >= MAX_PHOTOS:
            raise PhotoLimitError()
        photo_id = f"ph_{uuid.uuid4().hex}"
        photos.insert_one({
            "photo_id": photo_id,
            "user_id": user_id,
            "content": content,
            "content_type": content_type,
            "created_at": datetime.datetime.utcnow(),
        })
        users.update_one({"user_id": user_id}, {"$push": {"photos": photo_id}})
        return photo_id

    def delete_photo(self, user_id: str, photo_id: str) -> bool:
        users = self._users()
        photos = self._photos()
        if users is None or photos is None:
            return False
        res = users.update_one({"user_id": user_id}, {"$pull": {"photos": photo_id}})
        photos.delete_one({"photo_id": photo_id, "user_id": user_id})
        return res.modified_count > 0

    def get_photo(self, photo_id: str) -> Optional[Tuple[bytes, str]]:
        photos = self._photos()
        if photos is None:
            return None
        doc = photos.find_one({"photo_id": photo_id})
        if not doc:
            return None
        return bytes(doc["content"]), doc.get("content_type", "image/jpeg")

    def list_live_profiles(self, exclude_user_id: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        col = self._users()
        if col is None:
            return []
        cursor = (
            col.find({"profile_live": True, "user_id": {"$ne": exclude_user_id}})
            .sort("updated_at", -1)
            .limit(limit)
        )
        return [anon_profile(d) for d in cursor]
