"""
Strict-Mongo smoke test: mongomock wrapped so Database/Collection truth-testing
raises, exactly like real pymongo. This is the regression for the production
500: `bool(db and ...)` in social.liked/connected passed under plain mongomock
(which allows bool()) but exploded on pymongo. Every Mongo-backed store gets
exercised through the real routes under these semantics.
"""
import datetime

import mongomock
from fastapi.testclient import TestClient


class StrictDB:
    """Delegates to a mongomock database but forbids truth-testing,
    mirroring pymongo's Database.__bool__ behavior."""

    def __init__(self, inner): self._inner = inner
    def __bool__(self):
        raise NotImplementedError(
            "Database objects do not implement truth value testing")
    def __getattr__(self, name): return getattr(self._inner, name)
    def __getitem__(self, name): return self._inner[name]


def make_app():
    client = mongomock.MongoClient()
    db = StrictDB(client.get_database("proxyapp"))
    import commons.db as cdb
    cdb.get_db = lambda: db
    cdb.get_client = lambda: client

    import core.explore as ex
    ex.default_embedder = lambda texts: [[0.2] * 8 for _ in texts]

    from webapp.app import create_app, build_deps
    return TestClient(create_app(build_deps()), raise_server_exceptions=True), db


def seed_user(db, uid, name):
    now = datetime.datetime.utcnow()
    db.users.insert_one({
        "user_id": uid, "email": f"{uid}@x", "password_hash": "x", "name": name,
        "age": 22, "bio": "", "city": "", "gender": "", "pseudonym": f"P-{name}",
        "avatar": {"bg": "#fff"}, "photos": [], "transcript_visibility": False,
        "profile_live": True, "proxy_mode": "mimic",
        "created_at": now, "updated_at": now})
    db.conversations.insert_one({"user_id": uid, "messages": [],
        "spc_raw": {"personality_scores": {"Extraversion": 4},
                    "value_scores": {"Power": 3, "Security": 4, "Hedonism": 5},
                    "context": {"loves": "music", "hates": "traffic"}}})
    db.qa_pairs.insert_one({"user_id": uid, "embedding": [0.1] * 8})


def test_two_profiles_explore_under_strict_mongo():
    """The exact production scenario: two live profiles, viewer hits explore."""
    client, db = make_app()
    seed_user(db, "u_a", "Alice")
    seed_user(db, "u_b", "Bob")
    h = {"X-User-Id": "u_a"}
    r = client.get("/api/explore", headers=h)
    assert r.status_code == 200, r.text
    assert [p["user_id"] for p in r.json()["profiles"]] == ["u_b"]

    # the full social arc under strict semantics too
    assert client.post("/api/likes/u_b", headers=h).json()["mutual"] is False
    r = client.get("/api/explore", headers=h).json()
    assert r["profiles"] == [], "liked candidates are excluded"
    hb = {"X-User-Id": "u_b"}
    assert client.post("/api/likes/u_a", headers=hb).json()["mutual"] is True
    assert client.get("/api/users/u_a", headers=hb).json()["anonymous"] is False
    assert client.post("/api/messages/u_a", json={"text": "hi"},
                       headers=hb).status_code == 200
    assert client.get("/api/connections", headers=h).json()["connections"][0]["user_id"] == "u_b"


if __name__ == "__main__":
    test_two_profiles_explore_under_strict_mongo()
    print("OK - strict-mongo smoke tests passed")
