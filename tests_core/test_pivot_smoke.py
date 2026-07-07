"""
Pivot smoke test -- offline. Covers the anonymous-social rework:
  1. browse returns ONLY pseudonym+avatar (no name/age/city/photos)
  2. stranger profile view is anonymous; leaks nothing
  3. proxy speaks as the pseudonym; prompt forbids real name/location and
     offers age/gender as shareable
  4. the full like arc: like -> incoming (anonymous) -> like back -> mutual ->
     full profiles unlock -> DMs allowed; DMs forbidden before mutual
  5. can't like yourself; likes to non-live targets 404
  6. dynamic follow-ups: at most one per main, engine denies a second
"""
from fastapi.testclient import TestClient

from core import (
    InterviewEngine, InMemoryInterviewStore, QuestionBank,
    ProxyEngine, ProxyDefinitionCache, InMemorySessionStore, ProxyResponse,
)
from webapp.app import create_app
from webapp.social import InMemorySocialStore
from webapp.users import InMemoryUserStore


class ILLM:
    def next_turn(self, *, model, messages):
        return {"response": "noted! next?", "need_followup": False}


class CapturingPLLM:
    def __init__(self): self.last = None
    def classify_and_reply(self, *, model, messages):
        self.last = messages
        return ProxyResponse(category="non_question", action="answer",
                             has_prior_knowledge=True, confidence="high",
                             extracted_question=None, response="hey!")


def make_client():
    users = InMemoryUserStore()
    pllm = CapturingPLLM()

    def resolve_identity(uid):
        doc = users.get_by_id(uid) or {}
        return {"display_name": doc.get("pseudonym") or "Anonymous",
                "age": doc.get("age"), "gender": doc.get("gender") or None}

    deps = {
        "interview": InterviewEngine(
            bank=QuestionBank([{"id": "Q1", "main_question": "q?", "followups": []}]),
            store=InMemoryInterviewStore(), llm=ILLM(), model="f"),
        "proxy": ProxyEngine(
            definitions=ProxyDefinitionCache(fetch_record=lambda u: {"messages": []},
                                             resolve_name=resolve_identity),
            sessions=InMemorySessionStore(), llm=pllm, model="f"),
        "users": users,
        "persist_gap": lambda g: True,
        "finalize_training": lambda s, b: None,
        "social": InMemorySocialStore(),
    }
    return TestClient(create_app(deps)), deps, pllm


def make_live_user(client, email, name, pseudonym=None, gender=None):
    r = client.post("/api/auth/signup", json={
        "email": email, "password": "password1234", "name": name, "age": 23}).json()
    h = {"Authorization": f"Bearer {r['token']}"}
    patch = {"city": "Ithaca, NY"}
    if pseudonym: patch["pseudonym"] = pseudonym
    if gender: patch["gender"] = gender
    client.patch("/api/users/me", json=patch, headers=h)
    client.post("/api/interview/message", json={"text": "hi"}, headers=h)   # asks Q1
    client.post("/api/interview/message", json={"text": "ans"}, headers=h)  # answers -> live
    return h, r["user_id"]


def test_browse_is_anonymous():
    client, _, _ = make_client()
    ha, alice = make_live_user(client, "a@x.com", "Alice Realname", "Mossy Otter")
    hb, bob = make_live_user(client, "b@x.com", "Bob Realname")
    cards = client.get("/api/users", headers=hb).json()["profiles"]
    card = next(c for c in cards if c["user_id"] == alice)
    assert card["pseudonym"] == "Mossy Otter" and "avatar" in card
    for leak in ("name", "age", "city", "photos", "bio", "email"):
        assert leak not in card, f"browse leaked {leak}"


def test_stranger_profile_is_anonymous():
    client, _, _ = make_client()
    ha, alice = make_live_user(client, "a@x.com", "Alice Realname", "Mossy Otter")
    hb, bob = make_live_user(client, "b@x.com", "Bob")
    p = client.get(f"/api/users/{alice}", headers=hb).json()
    assert p["anonymous"] is True and p["pseudonym"] == "Mossy Otter"
    assert p["you_liked"] is False and p["connected"] is False
    for leak in ("name", "age", "city", "photos", "bio"):
        assert leak not in p, f"anon profile leaked {leak}"
    # owner still sees their own full profile
    own = client.get(f"/api/users/{alice}", headers=ha).json()
    assert own["anonymous"] is False and own["name"] == "Alice Realname"


def test_proxy_speaks_as_pseudonym_with_anonymity_rules():
    client, _, pllm = make_client()
    ha, alice = make_live_user(client, "a@x.com", "Alice Realname",
                               "Mossy Otter", gender="female")
    hb, bob = make_live_user(client, "b@x.com", "Bob")
    client.post(f"/api/proxy/{alice}/message", json={"text": "hi, who is this?"}, headers=hb)
    sys = pllm.last[0]["content"]
    assert "You are Mossy Otter" in sys
    assert "Alice Realname" not in sys, "real name must never reach the model as identity"
    assert "NEVER reveal your real name" in sys
    assert "NEVER reveal your location" in sys
    assert "your age (23)" in sys and "your gender (female)" in sys


def test_full_like_arc_and_dm_gating():
    client, _, _ = make_client()
    ha, alice = make_live_user(client, "a@x.com", "Alice", "Mossy Otter")
    hb, bob = make_live_user(client, "b@x.com", "Bob", "Cobalt Fox")

    # DMs forbidden pre-connection
    assert client.post(f"/api/messages/{bob}", json={"text": "hi"},
                       headers=ha).status_code == 403

    # alice likes bob -> not mutual yet
    r = client.post(f"/api/likes/{bob}", headers=ha).json()
    assert r["mutual"] is False
    # bob's incoming shows alice ANONYMOUSLY
    conns = client.get("/api/connections", headers=hb).json()
    assert conns["connections"] == []
    inc = conns["incoming"]
    assert len(inc) == 1 and inc[0]["pseudonym"] == "Mossy Otter"
    assert "name" not in inc[0]
    # bob's view of alice shows likes_you
    p = client.get(f"/api/users/{alice}", headers=hb).json()
    assert p["likes_you"] is True and p["anonymous"] is True

    # bob likes back -> mutual
    r = client.post(f"/api/likes/{alice}", headers=hb).json()
    assert r["mutual"] is True
    # full profiles unlock both ways
    p = client.get(f"/api/users/{alice}", headers=hb).json()
    assert p["anonymous"] is False and p["name"] == "Alice" and p["connected"] is True
    conns = client.get("/api/connections", headers=ha).json()
    assert conns["incoming"] == []
    assert conns["connections"][0]["name"] == "Bob"

    # DMs now work both directions
    assert client.post(f"/api/messages/{bob}", json={"text": "hey bob!"},
                       headers=ha).status_code == 200
    msgs = client.get(f"/api/messages/{alice}", headers=hb).json()["messages"]
    assert msgs[-1]["text"] == "hey bob!" and msgs[-1]["from"] == alice


def test_like_guards():
    client, _, _ = make_client()
    ha, alice = make_live_user(client, "a@x.com", "Alice")
    assert client.post(f"/api/likes/{alice}", headers=ha).status_code == 400  # self
    assert client.post("/api/likes/u_ghost", headers=ha).status_code == 404   # not live


def test_dynamic_followup_max_one_per_main():
    class FollowupLLM:
        def next_turn(self, *, model, messages):
            return {"response": "wait really? tell me the story!", "need_followup": True}

    engine = InterviewEngine(
        bank=QuestionBank([
            {"id": "Q1", "main_question": "q1?", "followups": []},
            {"id": "Q2", "main_question": "q2?", "followups": []},
        ]),
        store=InMemoryInterviewStore(), llm=FollowupLLM(), model="f")
    engine.respond(user_id="u", text="hi")           # asks Q1 (no prev -> no fu allowed)
    state = engine.store.get_or_create("u")
    assert state.asked_ids == ["Q1"]
    engine.respond(user_id="u", text="i wrestle bears")   # LLM wants fu -> allowed once
    assert state.asked_ids == ["Q1"] and state.follow_up_ids == ["dynamic"]
    engine.respond(user_id="u", text="long story")        # LLM wants fu again -> DENIED
    assert state.asked_ids == ["Q1", "Q2"], "engine must cap at one follow-up per main"
    assert state.follow_up_ids == []


if __name__ == "__main__":
    test_browse_is_anonymous()
    test_stranger_profile_is_anonymous()
    test_proxy_speaks_as_pseudonym_with_anonymity_rules()
    test_full_like_arc_and_dm_gating()
    test_like_guards()
    test_dynamic_followup_max_one_per_main()
    print("OK - all pivot smoke tests passed")
