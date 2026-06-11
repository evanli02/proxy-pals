"""
Web API smoke test -- offline (fake LLMs, in-memory stores, stub visibility).

Proves the full request path through the real FastAPI routes:
  1. auth stub rejects missing identity
  2. interview turns advance and report profile_ready/progress
  3. proxy chat mints a conversation_id and continues it on reuse
  4. an ungrounded question triggers gap persistence via the route
  5. the visibility flag is snapshotted at conversation start and surfaced
"""
from fastapi.testclient import TestClient

from core import (
    InterviewEngine, InMemoryInterviewStore, QuestionBank,
    ProxyEngine, ProxyDefinitionCache, InMemorySessionStore, ProxyResponse,
)
from webapp.app import create_app


class ScriptedInterviewLLM:
    def next_turn(self, *, model, messages):
        return {"response": "cool! next q?", "need_followup": False, "follow_up_id": None}


class ScriptedProxyLLM:
    def classify_and_reply(self, *, model, messages):
        last = messages[-1]["content"]
        if "childhood pet" in last:
            return ProxyResponse(
                category="experiential", action="deflect",
                has_prior_knowledge=False, confidence="low",
                extracted_question="what was your childhood pet",
                response="hmm can't remember",
            )
        return ProxyResponse(
            category="non_question", action="answer",
            has_prior_knowledge=True, confidence="high",
            extracted_question=None, response=f"echo: {last}",
        )


def make_client():
    persisted_gaps = []

    class StubUsers:
        """Minimal user store: visibility map + everything else unused here."""
        def __init__(self):
            self.visibility = {"T": True, "U": False}
        def get_by_id(self, uid):
            return None  # treat T/U as legacy targets -> visibility fallback
        def get_visibility(self, uid):
            return self.visibility.get(uid, False)
        def set_profile_live(self, uid):
            pass
        def user_id_for_token(self, th):
            return None
    users = StubUsers()

    engines = {
        "interview": InterviewEngine(
            bank=QuestionBank([
                {"id": "Q1", "main_question": "Major?", "followups": []},
                {"id": "Q2", "main_question": "Pets?", "followups": []},
            ]),
            store=InMemoryInterviewStore(),
            llm=ScriptedInterviewLLM(), model="fake",
        ),
        "proxy": ProxyEngine(
            definitions=ProxyDefinitionCache(
                fetch_record=lambda uid: {"messages": []},
                resolve_name=lambda uid: f"User {uid}",
            ),
            sessions=InMemorySessionStore(),
            llm=ScriptedProxyLLM(), model="fake",
        ),
        "users": users,
        "persist_gap": lambda gap: persisted_gaps.append(gap) or True,
    }
    return TestClient(create_app(engines)), persisted_gaps, users.visibility


def test_auth_required():
    client, _, _ = make_client()
    r = client.post("/api/proxy/T/message", json={"text": "hi"})
    assert r.status_code == 401


def test_interview_progress_and_ready():
    client, _, _ = make_client()
    h = {"X-User-Id": "alice"}
    r1 = client.post("/api/interview/message", json={"text": "hi"}, headers=h).json()
    assert r1["asked_count"] == 1 and not r1["profile_ready"]
    r2 = client.post("/api/interview/message", json={"text": "cs"}, headers=h).json()
    assert r2["asked_count"] == 2 and not r2["profile_ready"]  # asked != answered
    r2 = client.post("/api/interview/message", json={"text": "pets!"}, headers=h).json()
    assert r2["profile_ready"]
    status = client.get("/api/interview/status", headers=h).json()
    assert status["profile_ready"] and status["total_main_questions"] == 2


def test_proxy_chat_conversation_continuity():
    client, _, _ = make_client()
    h = {"X-User-Id": "V"}
    r1 = client.post("/api/proxy/T/message", json={"text": "hello"}, headers=h).json()
    conv = r1["conversation_id"]
    assert conv.startswith("px_") and r1["reply"] == "echo: hello"
    r2 = client.post(
        "/api/proxy/T/message",
        json={"text": "again", "conversation_id": conv}, headers=h,
    ).json()
    assert r2["conversation_id"] == conv


def test_gap_persisted_through_route():
    client, gaps, _ = make_client()
    h = {"X-User-Id": "V"}
    client.post("/api/proxy/T/message",
                json={"text": "what was your childhood pet?"}, headers=h)
    assert len(gaps) == 1
    assert gaps[0].target_id == "T" and gaps[0].asked_by == "V"


def test_visibility_snapshot_consent_rule():
    client, _, visibility = make_client()
    h = {"X-User-Id": "V"}
    # T has visibility ON -> surfaced to viewer
    r = client.post("/api/proxy/T/message", json={"text": "hi"}, headers=h).json()
    assert r["target_visibility_on"] is True
    conv = r["conversation_id"]
    # T flips the toggle OFF mid-conversation; existing conversation keeps its snapshot
    visibility["T"] = False
    r2 = client.post("/api/proxy/T/message",
                     json={"text": "still here", "conversation_id": conv}, headers=h).json()
    assert r2["target_visibility_on"] is True, "snapshot must not change retroactively"
    # ...but a NEW conversation reflects the new state
    r3 = client.post("/api/proxy/T/message", json={"text": "new conv"}, headers=h).json()
    assert r3["target_visibility_on"] is False


if __name__ == "__main__":
    test_auth_required()
    test_interview_progress_and_ready()
    test_proxy_chat_conversation_continuity()
    test_gap_persisted_through_route()
    test_visibility_snapshot_consent_rule()
    print("OK - all web API smoke tests passed")
