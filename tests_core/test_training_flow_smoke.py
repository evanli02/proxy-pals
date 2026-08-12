"""
Training-flow smoke test -- offline. Covers the v3 question bank (identity
intake -> three topic conversations -> SPC survey), structured interview
turns, TIPI/PVQ scoring, the training compiler (incl. identity/topics and
anything-else QA decomposition), MBTI prompt injection, and the proxy card.
"""
from fastapi.testclient import TestClient

from core import (
    InterviewEngine, InMemoryInterviewStore, question_bank_v3,
    score_tipi, score_pvq, compile_training, record_to_definition,
    ProxyEngine, ProxyDefinitionCache, InMemorySessionStore, ProxyResponse,
    TOPIC_PRESETS,
)
from core.interview import TOPIC_MIN_FOLLOWUPS, TOPIC_MAX_FOLLOWUPS
from webapp.app import create_app
from webapp.users import InMemoryUserStore

BANK = question_bank_v3()

IDENTITY_ANSWERS = {
    "ID_NAME": "Alice",
    "ID_HOMETOWN": "Buffalo, NY",
    "ID_LOCATION": "Ithaca, NY",
    "ID_OCCUPATION": "CS student at Cornell",
    "ID_LANGUAGES": "English and a bit of Mandarin",
}


class ScriptedInterviewLLM:
    """need_followup always False: topic conversations still run their forced
    minimum follow-ups, then wrap."""
    def next_turn(self, *, model, messages):
        return {"response": "nice! next?", "need_followup": False, "follow_up_id": None}


def make_engine():
    return InterviewEngine(bank=BANK, store=InMemoryInterviewStore(),
                           llm=ScriptedInterviewLLM(), model="fake")


def likert(prefix, n, val=6):
    return {f"{prefix}_{i}": val for i in range(1, n + 1)}


STRUCTURED_ANSWERS = {
    "SPC_TIPI": likert("tipi", 10, 6),
    "SPC_PVQ": likert("pvq", 21, 5),
    "SPC_MBTI": "INTP",
    "SPC_LOVES": ["basketball", "ramen", "hiking", "jazz", "travel"],
    "SPC_HATES": ["traffic", "mosquitoes", "slow wifi", "mornings", "cilantro"],
    "SPC_WEEKDAY": "wake up, classes, gym, code, sleep " * 5,
    "SPC_WEEKEND": "sleep in, brunch, projects, friends, movies " * 5,
}

TOPIC_PICKS = ["What's your hottest take?", "What's your dream vacation?",
               "why cats are better than dogs"]


def run_full_interview(engine, uid="alice", mbti="INTP", anything_else=None):
    """Drive the whole v3 bank: identity intake, three topic conversations,
    then the structured survey; returns the last result."""
    r = engine.respond(user_id=uid, text="hi")
    answers = dict(STRUCTURED_ANSWERS)
    answers["SPC_MBTI"] = mbti
    picks = list(TOPIC_PICKS)
    for _ in range(300):
        if r.complete:
            break
        if r.question_payload is not None:
            qid = r.question_payload["question_id"]
            if r.question_payload["type"] == "topic_choice":
                r = engine.choose_topic(user_id=uid, question_id=qid,
                                        topic=picks.pop(0))
            else:
                r = engine.submit_answer(user_id=uid, question_id=qid,
                                         answer=answers.get(qid))
        else:
            state = engine.store.get_or_create(uid)
            last_q = state.asked_ids[-1] if state.asked_ids else ""
            if state.active_topic_id:
                text = "my topic answer"
            elif last_q in IDENTITY_ANSWERS:
                text = IDENTITY_ANSWERS[last_q]
            elif anything_else and last_q == "Q_ANYTHING_ELSE":
                text = anything_else
            else:
                text = "my answer"
            r = engine.respond(user_id=uid, text=text)
    return r


def test_bank_shape():
    assert BANK.main_count() == 16
    # identity intake: static, verbatim, never followed up
    assert BANK.asks_verbatim("ID_NAME") and not BANK.allows_followup("ID_NAME")
    assert not BANK.is_structured("ID_NAME") and not BANK.is_topic("ID_NAME")
    # topic cards
    assert BANK.is_topic("TOPIC_1") and not BANK.is_structured("TOPIC_1")
    p = BANK.payload("TOPIC_1")
    assert p["type"] == "topic_choice" and p["allow_custom"] is True
    assert p["options"] == TOPIC_PRESETS and len(p["options"]) >= 10
    # survey unchanged
    assert BANK.is_structured("SPC_TIPI") and BANK.is_structured("SPC_MBTI")
    p = BANK.payload("SPC_TIPI")
    assert len(p["items"]) == 10 and len(p["scale_labels"]) == 7
    assert len(BANK.payload("SPC_PVQ")["items"]) == 21
    assert BANK.payload("SPC_MBTI")["optional"] is True
    assert BANK.allows_followup("Q_ANYTHING_ELSE")


def test_validation():
    ok = BANK.validate_answer("SPC_TIPI", likert("tipi", 10, 7))
    assert ok["tipi_1"] == 7
    for bad in [likert("tipi", 9), likert("tipi", 10, 9), "not a dict"]:
        try:
            BANK.validate_answer("SPC_TIPI", bad)
            assert False, f"should have rejected {bad!r}"
        except ValueError:
            pass
    try:
        BANK.validate_answer("SPC_LOVES", ["a", "b"])
        assert False, "min_items not enforced"
    except ValueError:
        pass
    assert BANK.validate_answer("SPC_MBTI", None) is None  # optional skip


def test_identity_intake_is_verbatim_and_static():
    engine = make_engine()
    r = engine.respond(user_id="u", text="hi")
    # asked word-for-word, no LLM paraphrase
    assert r.reply_text == BANK.get("ID_NAME")["main_question"]
    r = engine.respond(user_id="u", text="Alice")
    assert r.reply_text == BANK.get("ID_HOMETOWN")["main_question"]
    state = engine.store.get_or_create("u")
    assert state.asked_ids == ["ID_NAME", "ID_HOMETOWN"]
    assert state.follow_up_ids == [], "identity questions never get follow-ups"


def test_topic_flow_followup_budget():
    engine = make_engine()
    uid = "topics"
    r = engine.respond(user_id=uid, text="hi")
    for qid in ["ID_NAME", "ID_HOMETOWN", "ID_LOCATION", "ID_OCCUPATION", "ID_LANGUAGES"]:
        r = engine.respond(user_id=uid, text=IDENTITY_ANSWERS[qid])
    assert r.question_payload["question_id"] == "TOPIC_1"

    r = engine.choose_topic(user_id=uid, question_id="TOPIC_1",
                            topic="What's your hottest take?")
    assert r.reply_text, "topic opening must ask something"
    state = engine.store.get_or_create(uid)
    assert state.active_topic_id == "TOPIC_1"
    assert state.structured_answers["TOPIC_1"] == "What's your hottest take?"

    # the scripted LLM never volunteers a follow-up, so exactly the forced
    # minimum are asked before the wrap advances to TOPIC_2
    turns = 0
    while state.active_topic_id and turns < 20:
        r = engine.respond(user_id=uid, text="my take")
        turns += 1
    assert state.asked_ids[-1] == "TOPIC_1"
    assert turns == TOPIC_MIN_FOLLOWUPS + 1  # 3 follow-ups + the wrap turn
    assert r.question_payload["question_id"] == "TOPIC_2"
    assert TOPIC_MIN_FOLLOWUPS < TOPIC_MAX_FOLLOWUPS

    # out-of-order / bad topic submissions are rejected
    try:
        engine.choose_topic(user_id=uid, question_id="TOPIC_3", topic="x")
        assert False, "out-of-order topic choice must raise"
    except ValueError:
        pass
    try:
        engine.choose_topic(user_id=uid, question_id="TOPIC_2", topic="   ")
        assert False, "empty topic must raise"
    except ValueError:
        pass


def test_topic_budget_cap_never_strands_a_question():
    """THE BUG: at the follow-up cap the LLM may still want to keep going --
    its question-shaped reply must be DROPPED, never shown right before the
    next-topic card (where the user can't answer it)."""
    class EagerLLM:
        def next_turn(self, *, model, messages):
            return {"response": "but wait, tell me more??", "need_followup": True}

    engine = InterviewEngine(bank=BANK, store=InMemoryInterviewStore(),
                             llm=EagerLLM(), model="fake")
    uid = "cap"
    engine.respond(user_id=uid, text="hi")
    for qid in ["ID_NAME", "ID_HOMETOWN", "ID_LOCATION", "ID_OCCUPATION", "ID_LANGUAGES"]:
        engine.respond(user_id=uid, text=IDENTITY_ANSWERS[qid])
    engine.choose_topic(user_id=uid, question_id="TOPIC_1", topic="japanese food")
    state = engine.store.get_or_create(uid)

    turns = 0
    r = None
    while state.active_topic_id and turns < 20:
        r = engine.respond(user_id=uid, text="my answer")
        turns += 1
    assert turns == TOPIC_MAX_FOLLOWUPS + 1, "cap: max follow-ups then one ending turn"
    # the ending turn advances to the next topic card...
    assert r.question_payload["question_id"] == "TOPIC_2"
    # ...WITHOUT surfacing the model's dangling follow-up question
    assert not r.reply_text, "a question must never be shown right before the card"
    followups = [m for m in state.messages
                 if (m.get("metadata") or {}).get("topic_followup")]
    assert len(followups) == TOPIC_MAX_FOLLOWUPS, "the extra question must not enter the transcript"


def test_structured_flow_and_completion():
    engine = make_engine()
    r = run_full_interview(engine)
    assert r.complete and r.profile_ready
    state = engine.store.get_or_create("alice")
    assert len(state.asked_ids) == 16
    assert state.structured_answers["SPC_MBTI"] == "INTP"
    assert state.structured_answers["TOPIC_3"] == TOPIC_PICKS[2]
    # batteries chained: PVQ card was issued directly after TIPI submission
    assert "SPC_TIPI" in state.asked_ids and "SPC_PVQ" in state.asked_ids


def test_tipi_scoring_hand_computed():
    # all 6s: each trait = (6 + (8-6)) / 2 = 4.0
    assert all(v == 4.0 for v in score_tipi(likert("tipi", 10, 6)).values())
    # targeted: extraversion high (item1=7, item6=1 -> (7 + 7)/2 = 7)
    a = likert("tipi", 10, 4); a["tipi_1"] = 7; a["tipi_6"] = 1
    s = score_tipi(a)
    assert s["Extraversion"] == 7.0 and s["Agreeableness"] == 4.0
    # pipeline-compatible keys
    assert set(s) == {"Extraversion", "Agreeableness", "Conscientiousness",
                      "Negative Emotionality", "Open-Mindedness"}


def test_pvq_scoring_hand_computed():
    a = likert("pvq", 21, 4)
    a["pvq_1"], a["pvq_11"] = 7, 5          # Self-Direction = 6.0
    a["pvq_3"], a["pvq_8"], a["pvq_19"] = 7, 7, 1   # Universalism = 5.0
    s = score_pvq(a)
    assert s["Self-Direction"] == 6.0
    assert s["Universalism"] == 5.0
    assert s["Hedonism"] == 4.0
    assert len(s) == 10


def test_compiler_identity_topics_and_mbti_injection():
    engine = make_engine()
    run_full_interview(
        engine, anything_else="I'm a twin! Also I lived in Japan for two years "
        "and I'm fluent in Japanese. I collect vinyl records.")
    state = engine.store.get_or_create("alice")

    compiled = compile_training(
        state, BANK,
        personality_generator=lambda p, v: "generated personality profile",
        anything_else_decomposer=lambda text: [
            {"question": "Do you have any siblings?", "answer": "I'm a twin!"},
            {"question": "Have you lived abroad?",
             "answer": "I lived in Japan for two years and I'm fluent in Japanese."},
        ],
    )
    assert compiled.personality_text == "generated personality profile"
    assert compiled.mbti == "INTP"
    assert compiled.identity == IDENTITY_ANSWERS_BY_FIELD
    assert compiled.topics == TOPIC_PICKS
    ctx = compiled.spc_raw["context"]
    assert "basketball" in ctx["loves"] and "traffic" in ctx["hates"]
    assert ctx["weekday"] and ctx["weekend"]
    assert compiled.spc_raw["personality_scores"]["Extraversion"] == 4.0

    # decomposed pairs are schema-exact qa_pairs items
    ae = [i for i in compiled.qa_items if i["q_msg_id"].startswith("ae_")]
    assert len(ae) == 2
    item = ae[0]
    for key in ["_id", "qa_id", "user_id", "channel_id", "q_msg_id", "a_msg_id",
                "question_text", "answer_text", "qa_text", "created_at"]:
        assert key in item, f"missing {key}"
    assert item["_id"].startswith("qa_") and item["qa_text"].startswith("Q: ")
    # transcript pairs were extracted too (existing extractor path)
    assert len(compiled.qa_items) > 2

    # the compiled record drives the proxy prompt: MBTI + shareable identity
    record = {"personality": compiled.personality_text, "mbti": compiled.mbti,
              "spc_raw": compiled.spc_raw, "messages": compiled.messages,
              "identity": compiled.identity, "topics": compiled.topics}
    definition = record_to_definition("alice", record, "Cobalt Fox")
    prompt = definition.system_prompt()
    assert "Your MBTI personality type is INTP" in prompt
    assert "generated personality profile" in prompt
    assert "basketball" in prompt  # loves -> spc context section
    # identity facts injected, shareable; the real name is stripped
    assert "WHO YOU ARE" in prompt
    assert "Ithaca, NY" in prompt and "Buffalo, NY" in prompt
    assert "CS student at Cornell" in prompt
    assert "Your real name is the ONLY thing that's off-limits" in prompt
    who = prompt.split("WHO YOU ARE")[1].split("\n\n")[0]
    assert "Alice" not in who, "real name must never enter the identity facts"

    # no MBTI -> no injection sentence
    record2 = dict(record); record2["mbti"] = None
    assert "Your MBTI personality type" not in record_to_definition(
        "a", record2, "A").system_prompt()


IDENTITY_ANSWERS_BY_FIELD = {
    "name": "Alice",
    "hometown": "Buffalo, NY",
    "location": "Ithaca, NY",
    "occupation": "CS student at Cornell",
    "languages": "English and a bit of Mandarin",
}


def test_api_round_trip_with_topics():
    users = InMemoryUserStore()
    finalized = []
    deps = {
        "interview": make_engine(),
        "proxy": ProxyEngine(
            definitions=ProxyDefinitionCache(fetch_record=lambda u: {"messages": []},
                                             resolve_name=lambda u: u),
            sessions=InMemorySessionStore(), llm=None, model="fake"),
        "users": users,
        "persist_gap": lambda g: True,
        "finalize_training": lambda state, bank: finalized.append(state.user_id),
    }
    client = TestClient(create_app(deps))
    s = client.post("/api/auth/signup", json={
        "email": "a@x.com", "password": "passwordpass", "name": "Al", "age": 22}).json()
    h = {"Authorization": f"Bearer {s['token']}"}

    # new accounts default to the Improv speaking style
    assert client.get("/api/users/me", headers=h).json()["proxy_mode"] == "free"

    # identity intake: five static questions, then the first topic card
    r = client.post("/api/interview/message", json={"text": "hi"}, headers=h).json()
    assert r["reply"] == BANK.get("ID_NAME")["main_question"]
    for _ in range(10):
        if r.get("question"):
            break
        r = client.post("/api/interview/message", json={"text": "answer"}, headers=h).json()
    assert r["question"]["question_id"] == "TOPIC_1"
    assert r["question"]["type"] == "topic_choice"
    assert r["question"]["options"] == TOPIC_PRESETS

    # a free-text message while the card is pending re-issues the card
    again = client.post("/api/interview/message", json={"text": "hello?"}, headers=h).json()
    assert again["question"]["question_id"] == "TOPIC_1"

    # choosing a topic opens the conversation
    r = client.post("/api/interview/topic", headers=h,
                    json={"question_id": "TOPIC_1", "topic": "What's your hottest take?"}).json()
    assert r["reply"] and not r.get("question")

    # invalid: choosing the wrong topic card -> 422
    bad = client.post("/api/interview/topic", headers=h,
                      json={"question_id": "TOPIC_3", "topic": "x"})
    assert bad.status_code == 422

    # talk through all three topics until the TIPI card shows up
    picks = iter(TOPIC_PICKS[1:])
    for _ in range(60):
        if r.get("question") and r["question"]["question_id"] == "SPC_TIPI":
            break
        if r.get("question") and r["question"]["type"] == "topic_choice":
            r = client.post("/api/interview/topic", headers=h,
                            json={"question_id": r["question"]["question_id"],
                                  "topic": next(picks)}).json()
        else:
            r = client.post("/api/interview/message", json={"text": "answer"}, headers=h).json()
    assert r["question"]["question_id"] == "SPC_TIPI"
    assert len(r["question"]["items"]) == 10

    # invalid battery -> 422 with reason
    bad = client.post("/api/interview/answer", headers=h,
                      json={"question_id": "SPC_TIPI", "answer": {"tipi_1": 3}})
    assert bad.status_code == 422

    # valid -> next card (PVQ) comes straight back
    good = client.post("/api/interview/answer", headers=h,
                       json={"question_id": "SPC_TIPI", "answer": likert("tipi", 10)})
    assert good.status_code == 200
    assert good.json()["question"]["question_id"] == "SPC_PVQ"

    # finish everything; finalize hook fires exactly once and profile goes live
    answers = {
        "SPC_PVQ": likert("pvq", 21), "SPC_MBTI": None,
        "SPC_LOVES": ["a", "b", "c", "d", "e"], "SPC_HATES": ["f", "g", "h", "i", "j"],
        "SPC_WEEKDAY": "routine text", "SPC_WEEKEND": "routine text",
    }
    r = good.json()
    for _ in range(60):
        if r["profile_ready"]:
            break
        if r.get("question"):
            qid = r["question"]["question_id"]
            r = client.post("/api/interview/answer", headers=h,
                            json={"question_id": qid, "answer": answers.get(qid)}).json()
        else:
            r = client.post("/api/interview/message", json={"text": "answer"},
                            headers=h).json()
    assert r["profile_ready"]
    assert finalized == [s["user_id"]]
    assert client.get("/api/users/me", headers=h).json()["profile_live"] is True
    # further turns don't re-finalize
    client.get("/api/interview/status", headers=h)
    assert finalized == [s["user_id"]]


def test_proxy_card_endpoint():
    users = InMemoryUserStore()
    record = {
        "identity": {"name": "Alice", "hometown": "Buffalo, NY",
                     "location": "Ithaca, NY", "occupation": "CS student"},
        "topics": ["What's your hottest take?", "dream vacations"],
        "spc_raw": {"context": {"loves": "basketball, ramen, hiking, jazz, travel"}},
        "messages": [],
    }
    deps = {
        "interview": make_engine(),
        "proxy": ProxyEngine(
            definitions=ProxyDefinitionCache(fetch_record=lambda u: record,
                                             resolve_name=lambda u: "Cobalt Fox"),
            sessions=InMemorySessionStore(), llm=None, model="fake"),
        "users": users,
        "persist_gap": lambda g: True,
        "fetch_training_record": lambda uid: record,
    }
    client = TestClient(create_app(deps))
    s = client.post("/api/auth/signup", json={
        "email": "a@x.com", "password": "passwordpass", "name": "Al", "age": 22}).json()
    h = {"Authorization": f"Bearer {s['token']}"}

    # self-view always allowed
    card = client.get(f"/api/proxy/{s['user_id']}/card", headers=h).json()
    assert card["age"] == 22
    assert card["location"] == "Ithaca, NY" and card["hometown"] == "Buffalo, NY"
    assert card["occupation"] == "CS student"
    assert card["interests"] == ["basketball", "ramen", "hiking", "jazz", "travel"]
    assert card["topics"] == ["What's your hottest take?", "dream vacations"]
    assert "name" not in card and "Alice" not in str(card)

    # a non-live target 404s for strangers
    s2 = client.post("/api/auth/signup", json={
        "email": "b@x.com", "password": "passwordpass", "name": "Bo", "age": 30}).json()
    h2 = {"Authorization": f"Bearer {s2['token']}"}
    assert client.get(f"/api/proxy/{s['user_id']}/card", headers=h2).status_code == 404
    # ...and works once live
    users.set_profile_live(s["user_id"])
    assert client.get(f"/api/proxy/{s['user_id']}/card", headers=h2).status_code == 200


def test_rag_context_injected_into_proxy_prompt():
    captured = {}

    class CapturingLLM:
        def classify_and_reply(self, *, model, messages):
            captured["messages"] = messages
            return ProxyResponse(
                category="experiential", action="answer",
                has_prior_knowledge=True, confidence="high",
                extracted_question=None, response="i lived in japan!",
            )

    engine = ProxyEngine(
        definitions=ProxyDefinitionCache(
            fetch_record=lambda u: {"messages": []}, resolve_name=lambda u: "Alice"),
        sessions=InMemorySessionStore(),
        llm=CapturingLLM(), model="fake",
        retriever=lambda uid, q: [
            {"qa_text": "Q: Have you lived abroad?\nA: I lived in Japan for two years."}],
    )
    engine.respond(viewer_id="V", target_id="T", conversation_id="c",
                   text="have you ever lived abroad?")
    sys_blocks = [m for m in captured["messages"] if m["role"] == "system"]
    assert len(sys_blocks) == 2, "definition prompt + RAG block expected"
    assert "lived in Japan" in sys_blocks[1]["content"]
    assert "RELEVANT THINGS YOU'VE SHARED BEFORE" in sys_blocks[1]["content"]

    # no retriever -> single system block (tests/default unchanged)
    engine2 = ProxyEngine(
        definitions=ProxyDefinitionCache(
            fetch_record=lambda u: {"messages": []}, resolve_name=lambda u: "A"),
        sessions=InMemorySessionStore(), llm=CapturingLLM(), model="fake")
    engine2.respond(viewer_id="V", target_id="T", conversation_id="c2", text="hi")
    assert len([m for m in captured["messages"] if m["role"] == "system"]) == 1


def test_prompts():
    from core import get_interview_prompt, get_topic_prompt
    p_yes = get_interview_prompt("Next Q?", True, "Prev Q?", "i play saxophone")
    p_no = get_interview_prompt("Next Q?", False, "Prev Q?", "ok")
    assert "[FOLLOWUP_ALLOWED]\nyes" in p_yes and "[FOLLOWUP_ALLOWED]\nno" in p_no
    assert "tailored to the exact thing they said" in p_yes

    t_open = get_topic_prompt("What's your hottest take?", phase="opening")
    assert "What's your hottest take?" in t_open and "opening" in t_open
    t_must = get_topic_prompt("cats", phase="conversation",
                              must_followup=True, may_followup=True,
                              user_message="i love cats")
    assert "MUST_FOLLOWUP: yes" in t_must and "i love cats" in t_must
    t_wrap = get_topic_prompt("cats", phase="conversation",
                              must_followup=False, may_followup=False)
    assert "MAY_FOLLOWUP: no" in t_wrap


if __name__ == "__main__":
    test_bank_shape()
    test_validation()
    test_identity_intake_is_verbatim_and_static()
    test_topic_flow_followup_budget()
    test_topic_budget_cap_never_strands_a_question()
    test_structured_flow_and_completion()
    test_tipi_scoring_hand_computed()
    test_pvq_scoring_hand_computed()
    test_compiler_identity_topics_and_mbti_injection()
    test_api_round_trip_with_topics()
    test_proxy_card_endpoint()
    test_rag_context_injected_into_proxy_prompt()
    test_prompts()
    print("OK - all training-flow smoke tests passed")
