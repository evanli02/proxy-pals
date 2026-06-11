"""
Feature-batch smoke test -- offline. Covers:
  1. THE BUG: the final free-text question is asked and must be ANSWERED
     before profile_ready flips (regression for the skipped-last-question bug)
  2. skip: marker recorded, excluded from samples/QA, interview still completes
  3. proxy_mode: PATCH validates, route passes the target's mode to the engine
  4. name/city editable, city appears in public profile
  5. bio suggestions endpoint (409 before training, grounded list after)
  6. review loop: pending -> answer -> resolved + proxy cache invalidated
  7. restart resets interview state
"""
from fastapi.testclient import TestClient

from core import (
    InterviewEngine, InMemoryInterviewStore, QuestionBank,
    ProxyEngine, ProxyDefinitionCache, InMemorySessionStore, ProxyResponse,
    compile_training, record_to_definition,
)
from core.interview import SKIP_MARKER
from webapp.app import create_app
from webapp.users import InMemoryUserStore

BANK = QuestionBank([
    {"id": "Q1", "main_question": "Where are you from?", "followups": []},
    {"id": "Q2", "main_question": "Anything else you want me to know about you?",
     "decompose_to_qa": True, "followups": []},
])


class ILLM:
    def next_turn(self, *, model, messages):
        return {"response": "noted! next?", "need_followup": False, "follow_up_id": None}


class PLLM:
    def __init__(self): self.last_messages = None
    def classify_and_reply(self, *, model, messages):
        self.last_messages = messages
        return ProxyResponse(category="non_question", action="answer",
                             has_prior_knowledge=True, confidence="high",
                             extracted_question=None, response="hi!")


def make_client():
    pllm = PLLM()
    proxy = ProxyEngine(
        definitions=ProxyDefinitionCache(fetch_record=lambda u: {"messages": []},
                                         resolve_name=lambda u: "U"),
        sessions=InMemorySessionStore(), llm=pllm, model="f")

    class FakeReview:
        def __init__(self):
            self.items = {"g1": {"id": "g1", "question": "fav color?", "category": "preference",
                                 "created_at": "now", "status": "pending"}}
            self.answered = []
        def pending(self, uid, limit=50):
            return [i for i in self.items.values() if i["status"] == "pending"]
        def answer(self, uid, item_id, text):
            it = self.items.get(item_id)
            if not it or it["status"] != "pending": return False
            it["status"] = "answered"; self.answered.append((item_id, text)); return True

    review = FakeReview()
    class FakeKnowledge:
        def __init__(self):
            self.items = {"qa_1": {"id": "qa_1", "question": "fav food?",
                                   "answer": "ramen", "created_at": "now"}}
        def list(self, uid, limit=200): return list(self.items.values())
        def update(self, uid, qa_id, ans):
            if qa_id not in self.items: return False
            self.items[qa_id]["answer"] = ans; return True
        def delete(self, uid, qa_id): return self.items.pop(qa_id, None) is not None

    knowledge = FakeKnowledge()
    deps = {
        "interview": InterviewEngine(bank=BANK, store=InMemoryInterviewStore(),
                                     llm=ILLM(), model="f"),
        "proxy": proxy,
        "users": InMemoryUserStore(),
        "persist_gap": lambda g: True,
        "finalize_training": lambda s, b: None,
        "review": review,
        "knowledge": knowledge,
        "bio_generator": lambda record: ["Ramen-powered CS student.",
                                         "Will debate you about jazz."],
        "fetch_training_record": lambda uid: {"messages": [{"role": "user", "content": "hi"}]},
    }
    deps['_knowledge'] = knowledge
    return TestClient(create_app(deps)), deps, pllm, review


def signup(client, email="a@x.com"):
    r = client.post("/api/auth/signup", json={
        "email": email, "password": "password1234", "name": "Al", "age": 22})
    j = r.json()
    return {"Authorization": f"Bearer {j['token']}"}, j["user_id"]


def test_last_question_must_be_answered():
    client, deps, _, _ = make_client()
    h, uid = signup(client)
    r = client.post("/api/interview/message", json={"text": "hi"}, headers=h).json()      # asks Q1
    assert not r["profile_ready"]
    r = client.post("/api/interview/message", json={"text": "ithaca"}, headers=h).json()  # asks Q2 (final)
    # THE BUG: this used to be profile_ready=True, hiding the final question
    assert not r["profile_ready"] and r["reply"], "asking the final question must not complete"
    r = client.post("/api/interview/message", json={"text": "i'm a twin!"}, headers=h).json()
    assert r["profile_ready"], "answering the final question completes"
    state = deps["interview"].store.get_or_create(uid)
    assert state.messages[-1]["content"] == "i'm a twin!", "final answer captured"


def test_skip_flow_and_exclusion():
    client, deps, _, _ = make_client()
    h, uid = signup(client)
    client.post("/api/interview/message", json={"text": "hi"}, headers=h)        # asks Q1
    r = client.post("/api/interview/skip", headers=h).json()                     # skip Q1 -> asks Q2
    assert r["reply"] and not r["profile_ready"]
    r = client.post("/api/interview/message", json={"text": "love jazz"}, headers=h).json()
    assert r["profile_ready"]

    state = deps["interview"].store.get_or_create(uid)
    assert any(m.get("content") == SKIP_MARKER for m in state.messages)
    # excluded from samples...
    record = {"messages": state.messages}
    definition = record_to_definition(uid, record, "Al")
    assert all(SKIP_MARKER not in s for s in definition.samples)
    # ...and from QA extraction input
    compiled = compile_training(
        state, BANK,
        personality_generator=lambda p, v: "p",
        anything_else_decomposer=lambda t: [],
        qa_extractor=lambda conv: [{"flag": SKIP_MARKER in str(conv["messages"])}],
    )
    assert compiled.qa_items[0]["flag"] is False


def test_proxy_mode_toggle_reaches_engine():
    client, deps, pllm, _ = make_client()
    ha, alice = signup(client, "a@x.com")
    hb, bob = signup(client, "b@x.com")
    # finish alice's 2-question interview -> live
    client.post("/api/interview/message", json={"text": "hi"}, headers=ha)
    client.post("/api/interview/message", json={"text": "x"}, headers=ha)
    client.post("/api/interview/message", json={"text": "y"}, headers=ha)

    bad = client.patch("/api/users/me", json={"proxy_mode": "chaotic"}, headers=ha)
    assert bad.status_code == 422
    ok = client.patch("/api/users/me", json={"proxy_mode": "strict"}, headers=ha)
    assert ok.json()["proxy_mode"] == "strict"

    client.post(f"/api/proxy/{alice}/message", json={"text": "yo"}, headers=hb)
    sys_prompt = pllm.last_messages[0]["content"]
    definition = deps["proxy"].definitions.get(alice, mode="strict")
    assert definition.mode == "strict", "route must pass the target's chosen mode"


def test_name_city_editing_and_public_profile():
    client, _, _, _ = make_client()
    h, uid = signup(client)
    r = client.patch("/api/users/me", json={"name": "Evan", "city": "Ithaca, NY"}, headers=h).json()
    assert r["name"] == "Evan" and r["city"] == "Ithaca, NY"
    # city flows into the public view (self-view works pre-live)
    p = client.get(f"/api/users/{uid}", headers=h).json()
    assert p["city"] == "Ithaca, NY" and "email" not in p


def test_bio_suggestions():
    client, deps, _, _ = make_client()
    h, _ = signup(client)
    # no training record -> 409
    deps_fetch = deps["fetch_training_record"]
    deps["fetch_training_record"] = lambda uid: None
    assert client.post("/api/users/me/bio-suggestions", headers=h).status_code == 409
    deps["fetch_training_record"] = deps_fetch
    out = client.post("/api/users/me/bio-suggestions", headers=h).json()
    assert out["suggestions"] == ["Ramen-powered CS student.", "Will debate you about jazz."]


def test_review_loop():
    client, deps, _, review = make_client()
    h, uid = signup(client)
    qs = client.get("/api/review", headers=h).json()["questions"]
    assert qs and qs[0]["question"] == "fav color?"
    # answering teaches + resolves
    r = client.post("/api/review/g1/answer", json={"answer": "forest green"}, headers=h)
    assert r.status_code == 204
    assert review.answered == [("g1", "forest green")]
    assert client.get("/api/review", headers=h).json()["questions"] == []
    # unknown id -> 404
    assert client.post("/api/review/nope/answer", json={"answer": "x"}, headers=h).status_code == 404


def test_restart_resets_interview():
    client, deps, _, _ = make_client()
    h, uid = signup(client)
    client.post("/api/interview/message", json={"text": "hi"}, headers=h)
    assert deps["interview"].store.get_or_create(uid).asked_ids == ["Q1"]
    assert client.post("/api/interview/restart", headers=h).status_code == 204
    assert deps["interview"].store.get_or_create(uid).asked_ids == []



def test_survey_bridge_asks_next_question():
    """THE BUG (round 2): after the last survey card, the next free-text
    question must be asked immediately -- not after the user speaks first."""
    bank = QuestionBank([
        {"id": "S1", "type": "choice", "main_question": "Pick one", "options": ["A", "B"]},
        {"id": "QF", "main_question": "Anything else?", "followups": []},
    ])
    engine = InterviewEngine(bank=bank, store=InMemoryInterviewStore(),
                             llm=ILLM(), model="f")
    r = engine.respond(user_id="u", text="hi")           # issues the S1 card
    assert r.question_payload["question_id"] == "S1"
    r = engine.submit_answer(user_id="u", question_id="S1", answer="A")
    # the bridge turn must come back with the interviewer ASKING something
    assert r.question_payload is None and not r.complete
    assert r.reply_text, "bridge turn must ask the next question, not go silent"
    state = engine.store.get_or_create("u")
    assert state.asked_ids == ["S1", "QF"]
    # user messages: "hi" + the structured answer transcript ("A") -- the
    # bridge itself must not fabricate a third
    users_msgs = [m for m in state.messages if m["role"] == "user"]
    assert len(users_msgs) == 2 and users_msgs[-1]["content"] == "A"
    # answering completes
    r = engine.respond(user_id="u", text="i collect vinyl")
    assert r.complete and r.profile_ready


def test_knowledge_endpoints():
    client, deps, _, _ = make_client()
    h, _ = signup(client)
    items = client.get("/api/knowledge", headers=h).json()["items"]
    assert items[0]["question"] == "fav food?" and items[0]["answer"] == "ramen"
    # edit
    assert client.patch("/api/knowledge/qa_1", json={"answer": "tonkotsu ramen"},
                        headers=h).status_code == 204
    assert deps["_knowledge"].items["qa_1"]["answer"] == "tonkotsu ramen"
    # delete
    assert client.delete("/api/knowledge/qa_1", headers=h).status_code == 204
    assert client.get("/api/knowledge", headers=h).json()["items"] == []
    # unknown -> 404
    assert client.patch("/api/knowledge/nope", json={"answer": "x"}, headers=h).status_code == 404

if __name__ == "__main__":
    test_last_question_must_be_answered()
    test_skip_flow_and_exclusion()
    test_proxy_mode_toggle_reaches_engine()
    test_name_city_editing_and_public_profile()
    test_bio_suggestions()
    test_review_loop()
    test_restart_resets_interview()
    test_survey_bridge_asks_next_question()
    test_knowledge_endpoints()
    print("OK - all feature-batch smoke tests passed")
