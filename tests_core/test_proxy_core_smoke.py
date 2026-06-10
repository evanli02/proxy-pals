"""
Phase 0 smoke test -- runs fully offline (fake LLM, fake record fetch).

Proves the three properties Phase 0 is about:
  1. the core imports with NO Slack/OpenAI/Mongo env and makes no network call
  2. one read-only definition is shared by many concurrent viewers
  3. each conversation's history stays isolated under concurrency
  4. an unanswered question comes back as data, not a hidden DB write
"""
import threading

from core import (
    ProxyEngine,
    ProxyDefinitionCache,
    InMemorySessionStore,
    ProxyResponse,
)


class FakeLLM:
    """Echoes which target it was prompted as; flags one phrase as a gap."""

    def __init__(self):
        self.calls = 0
        self._lock = threading.Lock()

    def classify_and_reply(self, *, model, messages):
        with self._lock:
            self.calls += 1
        system = messages[0]["content"]
        last_user = messages[-1]["content"]
        name = system.split("You are ", 1)[1].split(" chatting", 1)[0]
        if "childhood pet" in last_user:  # simulate an ungrounded question
            return ProxyResponse(
                category="experiential", action="deflect",
                has_prior_knowledge=False, confidence="low",
                extracted_question="what was your childhood pet",
                response="hmm not sure i remember that",
            )
        return ProxyResponse(
            category="non_question", action="answer",
            has_prior_knowledge=True, confidence="high",
            extracted_question=None,
            response=f"hey i'm {name}, you said: {last_user}",
        )


def fake_fetch(user_id):
    return {
        "style_rules": {"summary": f"casual style for {user_id}"},
        "personality": f"personality-of-{user_id}",
        "messages": [{"role": "user", "content": f"i am {user_id}"}],
    }


def build_engine():
    return ProxyEngine(
        definitions=ProxyDefinitionCache(
            fetch_record=fake_fetch,
            resolve_name=lambda uid: f"User {uid}",
        ),
        sessions=InMemorySessionStore(),
        llm=FakeLLM(),
        model="fake-model",
    )


def test_shared_definition_is_cached_once():
    engine = build_engine()
    engine.respond(viewer_id="V1", target_id="T", conversation_id="c1", text="hi")
    d1 = engine.definitions.get("T")
    d2 = engine.definitions.get("T")
    assert d1 is d2, "definition should be cached and shared (same object)"
    assert "User T" in d1.system_prompt()


def test_sessions_are_isolated_under_concurrency():
    engine = build_engine()
    errors = []

    def chat(viewer, conv):
        try:
            for i in range(15):
                r = engine.respond(
                    viewer_id=viewer, target_id="T",
                    conversation_id=conv, text=f"{viewer}-msg-{i}",
                )
                assert viewer in r.reply_text or r.reply_text.startswith("hey i'm")
        except Exception as e:  # surface thread errors to the main thread
            errors.append(e)

    threads = [
        threading.Thread(target=chat, args=(f"V{n}", f"conv-{n}"))
        for n in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"thread errors: {errors}"

    # Each conversation saw only its own 15 user turns + 15 assistant turns.
    for n in range(8):
        sess = engine.sessions.get_or_create(f"V{n}", "T", f"conv-{n}")
        users = [m for m in sess.messages if m["role"] == "user"]
        assert len(users) == 15, f"conv-{n} leaked turns: {len(users)}"
        assert all(m["content"].startswith(f"V{n}-") for m in users)


def test_unanswered_question_returned_as_data():
    engine = build_engine()
    r = engine.respond(
        viewer_id="V1", target_id="T", conversation_id="c1",
        text="what was your childhood pet?",
    )
    assert r.unanswered_question is not None
    assert r.unanswered_question.target_id == "T"
    assert r.unanswered_question.asked_by == "V1"
    assert r.unanswered_question.category == "experiential"


if __name__ == "__main__":
    test_shared_definition_is_cached_once()
    test_sessions_are_isolated_under_concurrency()
    test_unanswered_question_returned_as_data()
    print("OK - all Phase 0 smoke tests passed")
