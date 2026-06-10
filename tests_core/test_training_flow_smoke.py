"""
Training-flow smoke test -- offline. Covers the v2 question bank, structured
interview turns, TIPI/PVQ scoring, the training compiler (incl. anything-else
QA decomposition), and MBTI prompt injection.
"""
from fastapi.testclient import TestClient

from core import (
    InterviewEngine, InMemoryInterviewStore, question_bank_v2,
    score_tipi, score_pvq, compile_training, record_to_definition,
    ProxyEngine, ProxyDefinitionCache, InMemorySessionStore, ProxyResponse,
)
from webapp.app import create_app
from webapp.users import InMemoryUserStore

BANK = question_bank_v2()


class ScriptedInterviewLLM:
    def next_turn(self, *, model, messages):
        return {"response": "nice! next?", "need_followup": False, "follow_up_id": None}


def make_engine():
    return InterviewEngine(bank=BANK, store=InMemoryInterviewStore(),
                           llm=ScriptedInterviewLLM(), model="fake")


def likert(prefix, n, val=6):
    return {f"{prefix}_{i}": val for i in range(1, n + 1)}


def run_full_interview(engine, uid="alice", mbti="INTP", anything_else=None):
    """Drive the whole v2 bank: free text until a structured card appears,
    then submit structured answers; returns the last result."""
    r = engine.respond(user_id=uid, text="hi")
    answers = {
        "SPC_TIPI": likert("tipi", 10, 6),
        "SPC_PVQ": likert("pvq", 21, 5),
        "SPC_MBTI": mbti,
        "SPC_LOVES": ["basketball", "ramen", "hiking", "jazz", "travel"],
        "SPC_HATES": ["traffic", "mosquitoes", "slow wifi", "mornings", "cilantro"],
        "SPC_WEEKDAY": "wake up, classes, gym, code, sleep " * 5,
        "SPC_WEEKEND": "sleep in, brunch, projects, friends, movies " * 5,
    }
    for _ in range(200):
        if r.complete:
            break
        if r.question_payload is not None:
            qid = r.question_payload["question_id"]
            r = engine.submit_answer(user_id=uid, question_id=qid,
                                     answer=answers.get(qid))
        else:
            text = anything_else if (
                anything_else and engine.store.get_or_create(uid).asked_ids
                and engine.store.get_or_create(uid).asked_ids[-1] == "Q_ANYTHING_ELSE"
            ) else "my answer"
            r = engine.respond(user_id=uid, text=text)
    return r


def test_bank_shape():
    assert BANK.main_count() == 26
    assert BANK.is_structured("SPC_TIPI") and BANK.is_structured("SPC_MBTI")
    assert not BANK.is_structured("Q_PETS")
    p = BANK.payload("SPC_TIPI")
    assert len(p["items"]) == 10 and len(p["scale_labels"]) == 7
    assert len(BANK.payload("SPC_PVQ")["items"]) == 21
    assert BANK.payload("SPC_MBTI")["optional"] is True


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


def test_structured_flow_and_completion():
    engine = make_engine()
    r = run_full_interview(engine)
    assert r.complete and r.profile_ready
    state = engine.store.get_or_create("alice")
    assert len(state.asked_ids) == 26
    assert state.structured_answers["SPC_MBTI"] == "INTP"
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


def test_compiler_and_mbti_injection():
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

    # the compiled record drives the proxy prompt, MBTI included
    record = {"personality": compiled.personality_text, "mbti": compiled.mbti,
              "spc_raw": compiled.spc_raw, "messages": compiled.messages}
    definition = record_to_definition("alice", record, "Alice")
    prompt = definition.system_prompt()
    assert "Your MBTI personality type is INTP" in prompt
    assert "generated personality profile" in prompt
    assert "basketball" in prompt  # loves -> spc context section

    # no MBTI -> no injection sentence (the raw transcript may still mention
    # the type in conversation history; the injection is what must be absent)
    record2 = dict(record); record2["mbti"] = None
    assert "Your MBTI personality type" not in record_to_definition(
        "a", record2, "A").system_prompt()


def test_api_structured_round_trip():
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

    # walk free-text questions until the TIPI card shows up
    r = client.post("/api/interview/message", json={"text": "hi"}, headers=h).json()
    for _ in range(40):
        if r.get("question"):
            break
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



def test_interview_prompt_conditional_followups():
    from core import get_interview_prompt, question_bank_v2
    bank = question_bank_v2()
    # gender and pronouns are now separate questions
    assert bank.get("Q_GENDER") and bank.get("Q_PRONOUNS")
    assert "gender" in bank.get("Q_GENDER")["main_question"].lower()
    assert "pronouns" in bank.get("Q_PRONOUNS")["main_question"].lower()

    fus = bank.followups("Q_PETS", set())
    prompt = get_interview_prompt("Do you have any pets?", fus, "Where are you from?", "i have a corgi!")
    # conditions are rendered next to each follow-up
    assert "ask only if: they said they DO have pets" in prompt
    assert "ask only if: they said they do NOT have pets" in prompt
    # hard rules present
    assert "NEVER ask a follow-up whose answer the user has ALREADY given" in prompt
    assert "NOT A CHECKLIST" in prompt
    # empty followups don't blow up
    assert "(none available)" in get_interview_prompt("Q?", [], "", "hi")

if __name__ == "__main__":
    test_bank_shape()
    test_validation()
    test_structured_flow_and_completion()
    test_tipi_scoring_hand_computed()
    test_pvq_scoring_hand_computed()
    test_compiler_and_mbti_injection()
    test_api_structured_round_trip()
    test_rag_context_injected_into_proxy_prompt()
    test_interview_prompt_conditional_followups()
    print("OK - all training-flow smoke tests passed")
