"""
Explore smoke test -- offline, deterministic fake embeddings.

Covers:
  1. component scores behave (mean-centered values; semantic interest match)
  2. chips fire on strong matches and use the VIEWER's own terms
  3. ranking order: kindred > partial > opposite for a clear pool
  4. likes-you boost lifts a candidate and prepends its chip
  5. /api/explore: exclusions (self, connected, already-liked), chips in payload,
     graceful fallback when features are missing
"""
import datetime

from fastapi.testclient import TestClient

from core import (
    UserFeatures, build_user_features, score_pair, rank_candidates,
    InterviewEngine, InMemoryInterviewStore, QuestionBank,
    ProxyEngine, ProxyDefinitionCache, InMemorySessionStore, ProxyResponse,
)
from webapp.app import create_app
from webapp.social import InMemorySocialStore
from webapp.users import InMemoryUserStore

# deterministic "embeddings": fixed concept axes
_CONCEPTS = ["music", "outdoors", "food", "games", "gym", "art"]


def fake_embed_one(text: str):
    t = text.lower()
    v = [1.0 if c in t else 0.0 for c in _CONCEPTS]
    return v if any(v) else [0.1] * len(_CONCEPTS)


def fake_embedder(texts):
    return [fake_embed_one(t) for t in texts]


def feats(uid, loves, hates, values, weekend="chill weekend", qa_concept="music"):
    record = {"spc_raw": {
        "value_scores": values,
        "personality_scores": {"Open-Mindedness": 5, "Extraversion": 4,
                               "Agreeableness": 5, "Conscientiousness": 4,
                               "Negative Emotionality": 3},
        "context": {"loves": ", ".join(loves), "hates": ", ".join(hates),
                    "weekday": "class and work", "weekend": weekend},
    }}
    return build_user_features(uid, record, [fake_embed_one(qa_concept)], fake_embedder)


VALS_A = {"Self-Direction": 6, "Stimulation": 6, "Hedonism": 5, "Achievement": 3,
          "Power": 1, "Security": 2, "Conformity": 2, "Tradition": 2,
          "Benevolence": 6, "Universalism": 6}
VALS_OPP = {k: 8 - v for k, v in VALS_A.items()}


def test_components_and_chips():
    A = feats("A", ["live music shows", "outdoors hiking"], ["gym bros"], VALS_A)
    B = feats("B", ["music festivals", "outdoors camping"], ["gym culture"], VALS_A)
    comps, chips = score_pair(A, B)
    assert comps["values"] > 0.9                       # identical priorities
    assert comps["interests"] > 0.5                    # semantic love+hate match
    assert any(c.startswith("You both love") for c in chips)
    # chips use A's own phrasing, never B's item
    for c in chips:
        assert "festivals" not in c and "camping" not in c and "gym culture" not in c
    assert any("can't stand" in c for c in chips)


def test_ranking_order_and_likes_boost():
    A = feats("A", ["music", "outdoors"], ["gym"], VALS_A, qa_concept="music")
    kindred = feats("K", ["music concerts", "outdoors"], ["gym"], VALS_A, qa_concept="music")
    partial = feats("P", ["food", "outdoors"], ["art"], VALS_A, qa_concept="food")
    opposite = feats("O", ["gym", "games"], ["music"], VALS_OPP,
                     weekend="grinding at the gym", qa_concept="gym")
    ranked = rank_candidates(A, [opposite, partial, kindred])
    assert [r["user_id"] for r in ranked][0] == "K"
    assert [r["user_id"] for r in ranked][-1] == "O"
    # likes-you boost can lift partial over kindred and prepends the chip
    ranked = rank_candidates(A, [opposite, partial, kindred], likes_you={"P"})
    p = next(r for r in ranked if r["user_id"] == "P")
    assert p["chips"][0] == "Liked your standin"
    assert ranked[0]["user_id"] in ("P", "K")  # boosted into contention


def test_freshness_boost():
    A = feats("A", ["music"], ["gym"], VALS_A)
    old = feats("OLD", ["food"], ["art"], VALS_A)
    fresh = feats("NEW", ["food"], ["art"], VALS_A)
    old.created_at = datetime.datetime.utcnow() - datetime.timedelta(days=60)
    fresh.created_at = datetime.datetime.utcnow() - datetime.timedelta(days=1)
    ranked = rank_candidates(A, [old, fresh])
    assert ranked[0]["user_id"] == "NEW"


# ------------------------- API level ------------------------------------------

class ILLM:
    def next_turn(self, *, model, messages):
        return {"response": "next?", "need_followup": False}


class FakeExploreStore:
    def __init__(self): self.feats = {}
    def get(self, uid): return self.feats.get(uid)
    def rebuild(self, uid): return self.feats.get(uid)


def make_client():
    users = InMemoryUserStore()
    explore = FakeExploreStore()
    deps = {
        "interview": InterviewEngine(
            bank=QuestionBank([{"id": "Q1", "main_question": "q?", "followups": []}]),
            store=InMemoryInterviewStore(), llm=ILLM(), model="f"),
        "proxy": ProxyEngine(
            definitions=ProxyDefinitionCache(fetch_record=lambda u: {"messages": []},
                                             resolve_name=lambda u: "X"),
            sessions=InMemorySessionStore(), llm=None, model="f"),
        "users": users,
        "persist_gap": lambda g: True,
        "finalize_training": lambda s, b: None,
        "social": InMemorySocialStore(),
        "explore": explore,
    }
    return TestClient(create_app(deps)), deps, explore


def live_user(client, email, name, loves, explore, values=VALS_A):
    r = client.post("/api/auth/signup", json={
        "email": email, "password": "password1234", "name": name, "age": 22}).json()
    h = {"Authorization": f"Bearer {r['token']}"}
    client.post("/api/interview/message", json={"text": "hi"}, headers=h)
    client.post("/api/interview/message", json={"text": "ans"}, headers=h)
    explore.feats[r["user_id"]] = feats(r["user_id"], loves, ["gym"], values)
    return h, r["user_id"]


def test_explore_endpoint():
    client, deps, explore = make_client()
    ha, alice = live_user(client, "a@x.com", "Alice", ["music", "outdoors"], explore)
    hk, kindred = live_user(client, "k@x.com", "Kin", ["music concerts"], explore)
    ho, opp = live_user(client, "o@x.com", "Opp", ["games"], explore, values=VALS_OPP)
    hc, conn = live_user(client, "c@x.com", "Conn", ["music"], explore)
    hl, liked = live_user(client, "l@x.com", "Liked", ["music"], explore)

    # connect alice<->conn; alice already liked `liked`
    deps["social"].like(alice, conn); deps["social"].like(conn, alice)
    deps["social"].like(alice, liked)

    out = client.get("/api/explore", headers=ha).json()["profiles"]
    ids = [p["user_id"] for p in out]
    assert alice not in ids and conn not in ids and liked not in ids, "exclusions"
    assert ids[0] == kindred, "kindred spirit ranks first"
    assert "chips" in out[0] and isinstance(out[0]["chips"], list)
    for leak in ("name", "age", "city", "photos"):
        assert leak not in out[0], f"explore leaked {leak}"

    # incoming like boosts + chips it
    deps["social"].like(opp, alice)
    out = client.get("/api/explore", headers=ha).json()["profiles"]
    opp_card = next(p for p in out if p["user_id"] == opp)
    assert opp_card["chips"][0] == "Liked your standin"

    # fallback: viewer without features still gets a plain list
    explore.feats.pop(alice)
    out = client.get("/api/explore", headers=ha).json()["profiles"]
    assert len(out) == 2 and all("pseudonym" in p for p in out)



def test_legacy_data_shapes_never_crash():
    """Production regression: legacy records store loves as LISTS, scores as
    strings, mixed embedding dims, stringified created_at. Ranking must
    degrade, never raise."""
    legacy_record = {"spc_raw": {
        "value_scores": {"Power": "high", "Security": 4},     # string score
        "personality_scores": None,
        "context": {"loves": ["music", "hiking"], "hates": None},  # LIST loves
    }}
    f_legacy = build_user_features("L", legacy_record, [[0.1] * 4, [0.2] * 9],
                                   fake_embedder)   # mixed qa dims
    assert f_legacy.loves == ["music", "hiking"]
    f_legacy.created_at = "2026-06-01"              # stringified date

    A = feats("A", ["music"], ["gym"], VALS_A)
    none_record_feats = UserFeatures(user_id="N")    # empty features
    ranked = rank_candidates(A, [f_legacy, none_record_feats])
    assert {r["user_id"] for r in ranked} == {"L", "N"}

if __name__ == "__main__":
    test_components_and_chips()
    test_ranking_order_and_likes_boost()
    test_freshness_boost()
    test_explore_endpoint()
    test_legacy_data_shapes_never_crash()
    print("OK - all explore smoke tests passed")
