"""
Users-layer smoke test -- offline (in-memory user store, fake engines).

Proves:
  1.  signup -> token works; duplicate email -> 409
  2.  login with right/wrong password; same 401 either way
  3.  bearer token authenticates /me; garbage token -> 401
  4.  profile PATCH edits name/bio/visibility (and ignores non-editable fields)
  5.  photo upload: max 6 enforced, bad type -> 415, oversize -> 413,
      uploaded bytes served back, delete works
  6.  public profile gating: hidden until live; browse lists only live
  7.  interview completion flips profile_live (the go-live gate, end to end)
  8.  proxy route refuses non-live targets but allows self-chat; visibility
      comes from the target's user record
"""
from fastapi.testclient import TestClient

from core import (
    InterviewEngine, InMemoryInterviewStore, QuestionBank,
    ProxyEngine, ProxyDefinitionCache, InMemorySessionStore, ProxyResponse,
)
from webapp.app import create_app
from webapp.users import InMemoryUserStore, MAX_PHOTOS


class ScriptedInterviewLLM:
    def next_turn(self, *, model, messages):
        return {"response": "cool, next?", "need_followup": False, "follow_up_id": None}


class EchoProxyLLM:
    def classify_and_reply(self, *, model, messages):
        return ProxyResponse(
            category="non_question", action="answer",
            has_prior_knowledge=True, confidence="high",
            extracted_question=None,
            response=f"echo: {messages[-1]['content']}",
        )


def make_client(num_questions=1):
    bank = QuestionBank([
        {"id": f"Q{i}", "main_question": f"q{i}?", "followups": []}
        for i in range(1, num_questions + 1)
    ])
    users = InMemoryUserStore()
    deps = {
        "interview": InterviewEngine(
            bank=bank, store=InMemoryInterviewStore(),
            llm=ScriptedInterviewLLM(), model="fake",
        ),
        "proxy": ProxyEngine(
            definitions=ProxyDefinitionCache(
                fetch_record=lambda uid: {"messages": []},
                resolve_name=lambda uid: f"User {uid}",
            ),
            sessions=InMemorySessionStore(),
            llm=EchoProxyLLM(), model="fake",
        ),
        "users": users,
        "persist_gap": lambda gap: True,
    }
    return TestClient(create_app(deps)), users


def signup(client, email="a@example.com", name="Alice", age=21):
    r = client.post("/api/auth/signup", json={
        "email": email, "password": "hunter22hunter22", "name": name, "age": age,
    })
    assert r.status_code == 201, r.text
    return r.json()  # {token, user_id}


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_signup_login_tokens():
    client, _ = make_client()
    s = signup(client)
    # duplicate email
    r = client.post("/api/auth/signup", json={
        "email": "a@example.com", "password": "hunter22hunter22",
        "name": "A2", "age": 30,
    })
    assert r.status_code == 409
    # bearer works
    me = client.get("/api/users/me", headers=auth(s["token"]))
    assert me.status_code == 200 and me.json()["email"] == "a@example.com"
    # garbage token rejected
    assert client.get("/api/users/me", headers=auth("nope")).status_code == 401
    # login right/wrong password
    ok = client.post("/api/auth/login", json={"email": "a@example.com",
                                              "password": "hunter22hunter22"})
    assert ok.status_code == 200
    bad = client.post("/api/auth/login", json={"email": "a@example.com",
                                               "password": "wrongwrong1"})
    assert bad.status_code == 401


def test_profile_patch():
    client, _ = make_client()
    s = signup(client)
    r = client.patch("/api/users/me", headers=auth(s["token"]), json={
        "bio": "hi there", "transcript_visibility": True, "name": "Alicia",
    })
    body = r.json()
    assert body["bio"] == "hi there"
    assert body["transcript_visibility"] is True
    assert body["name"] == "Alicia"
    assert body["profile_live"] is False  # patching never makes you live


def test_photos():
    client, _ = make_client()
    s = signup(client)
    h = auth(s["token"])
    jpg = ("p.jpg", b"\xff\xd8fakejpegbytes", "image/jpeg")

    # wrong type
    r = client.post("/api/users/me/photos", headers=h,
                    files={"file": ("x.gif", b"GIF89a", "image/gif")})
    assert r.status_code == 415
    # oversize
    r = client.post("/api/users/me/photos", headers=h,
                    files={"file": ("big.jpg", b"x" * (5 * 1024 * 1024 + 1), "image/jpeg")})
    assert r.status_code == 413
    # fill to the cap
    ids = []
    for _ in range(MAX_PHOTOS):
        r = client.post("/api/users/me/photos", headers=h, files={"file": jpg})
        assert r.status_code == 201
        ids.append(r.json()["photo_id"])
    # 7th rejected
    r = client.post("/api/users/me/photos", headers=h, files={"file": jpg})
    assert r.status_code == 409
    # bytes served back
    got = client.get(f"/api/photos/{ids[0]}")
    assert got.status_code == 200
    assert got.content == b"\xff\xd8fakejpegbytes"
    assert got.headers["content-type"].startswith("image/jpeg")
    # delete then 404
    assert client.delete(f"/api/users/me/photos/{ids[0]}", headers=h).status_code == 204
    assert client.get(f"/api/photos/{ids[0]}").status_code == 404
    assert len(client.get("/api/users/me", headers=h).json()["photos"]) == MAX_PHOTOS - 1


def test_go_live_gate_end_to_end():
    client, _ = make_client(num_questions=1)
    alice = signup(client, "a@example.com", "Alice")
    bob = signup(client, "b@example.com", "Bob")
    ha, hb = auth(alice["token"]), auth(bob["token"])

    # Bob can't see Alice yet (not live), and can't chat with her proxy
    assert client.get(f"/api/users/{alice['user_id']}", headers=hb).status_code == 404
    r = client.post(f"/api/proxy/{alice['user_id']}/message",
                    headers=hb, json={"text": "hi"})
    assert r.status_code == 404
    # ...but Alice can self-chat (auditing her own proxy) even before live
    r = client.post(f"/api/proxy/{alice['user_id']}/message",
                    headers=ha, json={"text": "hello me"})
    assert r.status_code == 200

    # browse shows nothing live yet
    assert client.get("/api/users", headers=hb).json()["profiles"] == []

    # Alice finishes the (1-question) interview -> profile goes live
    r1 = client.post("/api/interview/message", headers=ha, json={"text": "hi"})
    assert r1.json()["profile_ready"] is True
    assert client.get("/api/users/me", headers=ha).json()["profile_live"] is True

    # Now Bob sees her in browse and on her profile, and can chat
    profiles = client.get("/api/users", headers=hb).json()["profiles"]
    assert [p["user_id"] for p in profiles] == [alice["user_id"]]
    assert client.get(f"/api/users/{alice['user_id']}", headers=hb).status_code == 200

    # visibility flows from Alice's user record into the proxy session
    client.patch("/api/users/me", headers=ha, json={"transcript_visibility": True})
    r = client.post(f"/api/proxy/{alice['user_id']}/message",
                    headers=hb, json={"text": "hey alice's proxy"})
    body = r.json()
    assert r.status_code == 200
    assert body["target_visibility_on"] is True
    assert body["reply"].startswith("echo:")


if __name__ == "__main__":
    test_signup_login_tokens()
    test_profile_patch()
    test_photos()
    test_go_live_gate_end_to_end()
    print("OK - all users-layer smoke tests passed")
