"""
Interview-engine smoke test -- fully offline (fake LLM, synthetic bank).

Proves:
  1. imports with no Slack/OpenAI/Mongo env
  2. main questions advance in order
  3. a follow-up stays on the same main and is recorded
  4. profile_ready flips true exactly when the bank is exhausted
  5. per-user interview state is isolated
"""
import threading

from core import (
    InterviewEngine,
    InMemoryInterviewStore,
    QuestionBank,
    run_interview_turn,
    InterviewState,
)

BANK = QuestionBank([
    {"id": "Q1", "main_question": "What major are you in?",
     "followups": [{"id": "F1a", "question": "What year are you in?"}]},
    {"id": "Q2", "main_question": "Do you have any pets?", "followups": []},
])


class ScriptedLLM:
    """need_followup is driven by whether the user said the magic word."""

    def next_turn(self, *, model, messages):
        last_user = messages[-1]["content"]
        if "followup-please" in last_user:
            return {"response": "oh nice, what year are you in?",
                    "need_followup": True, "follow_up_id": "F1a"}
        return {"response": "got it! next question...",
                "need_followup": False, "follow_up_id": None}


def build_engine():
    return InterviewEngine(
        bank=BANK, store=InMemoryInterviewStore(),
        llm=ScriptedLLM(), model="fake",
    )


def test_main_questions_advance_in_order():
    engine = build_engine()
    r1 = engine.respond(user_id="u1", text="hi")          # asks Q1
    state = engine.store.get_or_create("u1")
    assert state.asked_ids == ["Q1"]
    assert not r1.profile_ready

    r2 = engine.respond(user_id="u1", text="answering q1")  # advances to Q2
    assert state.asked_ids == ["Q1", "Q2"]
    assert r2.profile_ready, "bank exhausted after Q2 -> profile ready"

    r3 = engine.respond(user_id="u1", text="answering q2")  # nothing left
    assert r3.complete and r3.reply_text is None


def test_followup_stays_on_same_main():
    engine = build_engine()
    engine.respond(user_id="u2", text="hi")                       # Q1 asked
    engine.respond(user_id="u2", text="followup-please")          # records F1a
    state = engine.store.get_or_create("u2")
    assert state.asked_ids == ["Q1"], "follow-up must NOT advance the main"
    assert state.follow_up_ids == ["F1a"]
    # next non-followup turn advances to Q2 and clears follow-ups
    engine.respond(user_id="u2", text="ok moving on")
    assert state.asked_ids == ["Q1", "Q2"]
    assert state.follow_up_ids == []


def test_per_user_isolation_under_concurrency():
    engine = build_engine()
    errors = []

    def run(uid):
        try:
            engine.respond(user_id=uid, text="hi")
            engine.respond(user_id=uid, text="answer")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=run, args=(f"user{n}",)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"thread errors: {errors}"
    for n in range(10):
        st = engine.store.get_or_create(f"user{n}")
        assert st.asked_ids == ["Q1", "Q2"], f"user{n} state leaked: {st.asked_ids}"


def test_default_bank_loads_from_repo():
    from core import default_question_bank
    bank = default_question_bank()
    assert bank.main_count() >= 1
    assert bank.next_main(set())["id"] == "Q1"


if __name__ == "__main__":
    test_main_questions_advance_in_order()
    test_followup_stays_on_same_main()
    test_per_user_isolation_under_concurrency()
    test_default_bank_loads_from_repo()
    print("OK - all interview smoke tests passed")
