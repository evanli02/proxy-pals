"""
FastAPI app for the lo-fi web prototype.

Routes:
  POST /api/auth/signup                -- create account -> bearer token
  POST /api/auth/login                 -- email+password -> bearer token
  GET  /api/users/me                   -- own profile
  PATCH /api/users/me                  -- edit name/age/bio/transcript_visibility
  POST /api/users/me/photos            -- upload a photo (multipart, max 6)
  DELETE /api/users/me/photos/{photo_id}
  GET  /api/photos/{photo_id}          -- serve photo bytes
  GET  /api/users                      -- live profiles (lo-fi browse placeholder)
  GET  /api/users/{user_id}            -- public profile (404 unless live)
  POST /api/interview/message          -- onboarding interview turn
  GET  /api/interview/status           -- progress + profile_ready
  POST /api/proxy/{target_id}/message  -- viewer talks to target's proxy
  GET  /api/health

Run:
  uvicorn webapp.app:app --reload      (single worker; see core/mongo_stores.py)
  http://localhost:8000/docs           (auto-generated OpenAPI for the frontend)

AUTH: bearer tokens minted by signup/login, verified against the user store
(see webapp/auth.py for scope honesty -- swap for Clerk/Auth0 before public).
The `X-User-Id` header still works as a DEV FALLBACK when no Authorization
header is present; remove `_DEV_HEADER_FALLBACK` before any deployment.

Everything is injectable via `build_deps`, so tests run fully offline.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field

from core import InterviewEngine, ProxyEngine, compile_training, default_persist, question_bank_v2
from core.proxy_definition import ProxyDefinitionCache
from core.proxy_engine import default_retriever
from core.mongo_stores import (
    MongoInterviewStore,
    MongoSessionStore,
    persist_unanswered_question,
)
from webapp.auth import hash_password, hash_token, mint_token, verify_password
from webapp.users import (
    ALLOWED_PHOTO_TYPES,
    MAX_PHOTO_BYTES,
    MAX_PHOTOS,
    DuplicateEmailError,
    MongoUserStore,
    PhotoLimitError,
    own_profile,
    public_profile,
)

log = logging.getLogger("webapp")

import os as _os
_DEV_HEADER_FALLBACK = _os.environ.get("ALLOW_DEV_USER_HEADER", "true").lower() == "true"
# Set ALLOW_DEV_USER_HEADER=false on any shared deployment: the X-User-Id
# fallback lets anyone impersonate anyone and exists only for local testing.


# --- request/response models -------------------------------------------------

class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=80)
    age: int = Field(ge=18, le=120)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    token: str
    user_id: str


class ProfilePatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    age: Optional[int] = Field(default=None, ge=18, le=120)
    bio: Optional[str] = Field(default=None, max_length=2000)
    transcript_visibility: Optional[bool] = None


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    conversation_id: Optional[str] = None


class InterviewOut(BaseModel):
    reply: Optional[str]
    complete: bool
    profile_ready: bool
    asked_count: int
    total_main_questions: int
    # present when the next question is structured (likert battery / list /
    # long_text / choice) -- the UI renders this card instead of free chat
    question: Optional[Dict[str, Any]] = None
    # status only: role/content transcript so the UI can restore on reload
    transcript: Optional[list] = None


class StructuredAnswerIn(BaseModel):
    question_id: str
    answer: Any = None  # None allowed for optional questions (e.g. MBTI skip)


class ProxyOut(BaseModel):
    conversation_id: str
    reply: str
    target_visibility_on: bool


# --- app factory ----------------------------------------------------------------

def build_deps() -> Dict[str, Any]:
    """Default production wiring. Tests build their own with fakes."""
    users = MongoUserStore()

    def resolve_display_name(user_id: str) -> str:
        doc = users.get_by_id(user_id)
        return (doc or {}).get("name") or user_id

    proxy = ProxyEngine(
        definitions=ProxyDefinitionCache(resolve_name=resolve_display_name),
        sessions=MongoSessionStore(),
        retriever=default_retriever,
    )

    def finalize_training(state, bank):
        """On interview completion: compile -> persist -> invalidate proxy cache
        so the freshly trained definition is what viewers get immediately."""
        compiled = compile_training(state, bank)
        default_persist(compiled)
        proxy.definitions.invalidate(state.user_id)

    return {
        "interview": InterviewEngine(store=MongoInterviewStore(), bank=question_bank_v2()),
        "proxy": proxy,
        "users": users,
        "persist_gap": persist_unanswered_question,
        "finalize_training": finalize_training,
    }


def create_app(deps: Optional[Dict[str, Any]] = None) -> FastAPI:
    d = deps or build_deps()
    interview: InterviewEngine = d["interview"]
    proxy: ProxyEngine = d["proxy"]
    users = d["users"]
    persist_gap = d["persist_gap"]
    finalize_training = d.get("finalize_training") or (lambda state, bank: None)

    app = FastAPI(title="Proxy Social Prototype API", version="0.2.0")

    # --- identity -------------------------------------------------------------

    def get_current_user(
        authorization: Optional[str] = Header(default=None),
        x_user_id: Optional[str] = Header(default=None),
    ) -> str:
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization.split(" ", 1)[1].strip()
            user_id = users.user_id_for_token(hash_token(token))
            if user_id:
                return user_id
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        if _DEV_HEADER_FALLBACK and x_user_id:
            return x_user_id
        raise HTTPException(status_code=401, detail="Authorization required")

    @app.get("/api/health")
    def health():
        return {"ok": True}

    # --- auth -------------------------------------------------------------------

    @app.post("/api/auth/signup", response_model=TokenOut, status_code=201)
    def signup(body: SignupIn):
        try:
            doc = users.create_user(
                body.email, hash_password(body.password), body.name, body.age
            )
        except DuplicateEmailError:
            raise HTTPException(status_code=409, detail="Email already registered")
        token = mint_token()
        users.save_token(hash_token(token), doc["user_id"])
        return TokenOut(token=token, user_id=doc["user_id"])

    @app.post("/api/auth/login", response_model=TokenOut)
    def login(body: LoginIn):
        doc = users.get_by_email(body.email)
        if not doc or not verify_password(body.password, doc["password_hash"]):
            # same error either way: don't reveal which emails exist
            raise HTTPException(status_code=401, detail="Invalid email or password")
        token = mint_token()
        users.save_token(hash_token(token), doc["user_id"])
        return TokenOut(token=token, user_id=doc["user_id"])

    # --- own profile ---------------------------------------------------------------

    @app.get("/api/users/me")
    def me(user_id: str = Depends(get_current_user)):
        doc = users.get_by_id(user_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="User not found")
        return own_profile(doc)

    @app.patch("/api/users/me")
    def update_me(body: ProfilePatch, user_id: str = Depends(get_current_user)):
        doc = users.update_profile(user_id, body.model_dump())
        if doc is None:
            raise HTTPException(status_code=404, detail="User not found")
        return own_profile(doc)

    # --- photos ------------------------------------------------------------------------

    @app.post("/api/users/me/photos", status_code=201)
    async def upload_photo(
        file: UploadFile = File(...), user_id: str = Depends(get_current_user)
    ):
        if file.content_type not in ALLOWED_PHOTO_TYPES:
            raise HTTPException(
                status_code=415,
                detail=f"Unsupported type {file.content_type}; allowed: {sorted(ALLOWED_PHOTO_TYPES)}",
            )
        content = await file.read()
        if len(content) > MAX_PHOTO_BYTES:
            raise HTTPException(status_code=413, detail="Photo exceeds 5MB limit")
        try:
            photo_id = users.add_photo(user_id, content, file.content_type)
        except PhotoLimitError:
            raise HTTPException(status_code=409, detail=f"Photo limit is {MAX_PHOTOS}")
        except KeyError:
            raise HTTPException(status_code=404, detail="User not found")
        return {"photo_id": photo_id, "url": f"/api/photos/{photo_id}"}

    @app.delete("/api/users/me/photos/{photo_id}", status_code=204)
    def delete_photo(photo_id: str, user_id: str = Depends(get_current_user)):
        if not users.delete_photo(user_id, photo_id):
            raise HTTPException(status_code=404, detail="Photo not found")
        return Response(status_code=204)

    @app.get("/api/photos/{photo_id}")
    def get_photo(photo_id: str):
        result = users.get_photo(photo_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Photo not found")
        content, content_type = result
        return Response(content=content, media_type=content_type)

    # --- public profiles -----------------------------------------------------------------

    @app.get("/api/users")
    def browse(user_id: str = Depends(get_current_user)):
        """Lo-fi discovery placeholder: live profiles, newest first.
        Real browse (filters, pagination) is Phase 3."""
        return {"profiles": users.list_live_profiles(exclude_user_id=user_id)}

    @app.get("/api/users/{target_id}")
    def view_profile(target_id: str, user_id: str = Depends(get_current_user)):
        doc = users.get_by_id(target_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="User not found")
        if target_id != user_id and not doc.get("profile_live"):
            # not browsable until the interview gate passes
            raise HTTPException(status_code=404, detail="User not found")
        return public_profile(doc)

    # --- interview (onboarding) -------------------------------------------------------

    def _interview_out(result, user_id: str) -> InterviewOut:
        state = interview.store.get_or_create(user_id)
        if result.profile_ready and not getattr(state, "_finalized", False):
            # the go-live gate: compile training, then make the profile live
            try:
                finalize_training(state, interview.bank)
            except Exception as e:
                log.error(f"finalize_training failed for {user_id}: {e}")
            users.set_profile_live(user_id)
            state._finalized = True
        return InterviewOut(
            reply=result.reply_text,
            complete=result.complete,
            profile_ready=result.profile_ready,
            asked_count=len(state.asked_ids),
            total_main_questions=interview.bank.main_count(),
            question=result.question_payload,
        )

    @app.post("/api/interview/message", response_model=InterviewOut)
    def interview_message(body: MessageIn, user_id: str = Depends(get_current_user)):
        result = interview.respond(user_id=user_id, text=body.text)
        return _interview_out(result, user_id)

    @app.post("/api/interview/answer", response_model=InterviewOut)
    def interview_answer(body: StructuredAnswerIn, user_id: str = Depends(get_current_user)):
        """Submit a structured answer (likert battery / list / long_text / choice)."""
        try:
            result = interview.submit_answer(
                user_id=user_id, question_id=body.question_id, answer=body.answer
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return _interview_out(result, user_id)

    @app.get("/api/interview/status", response_model=InterviewOut)
    def interview_status(user_id: str = Depends(get_current_user)):
        state = interview.store.get_or_create(user_id)
        ready = interview.bank.is_complete(set(state.asked_ids))
        # restore the pending structured card after a reload
        pending_q = None
        if not ready:
            nxt = interview.bank.next_main(set(state.asked_ids))
            if nxt is not None and interview.bank.is_structured(nxt["id"]) \
                    and state.pending_structured_id == nxt["id"]:
                pending_q = interview.bank.payload(nxt["id"])
        transcript = [
            {"role": m["role"], "content": m["content"]}
            for m in state.messages if m.get("role") and m.get("content")
        ]
        return InterviewOut(
            reply=None,
            complete=ready,
            profile_ready=ready,
            asked_count=len(state.asked_ids),
            total_main_questions=interview.bank.main_count(),
            question=pending_q,
            transcript=transcript,
        )

    # --- proxy chat -----------------------------------------------------------------------

    @app.post("/api/proxy/{target_id}/message", response_model=ProxyOut)
    def proxy_message(
        target_id: str,
        body: MessageIn,
        user_id: str = Depends(get_current_user),
    ):
        target = users.get_by_id(target_id)
        if target is not None:
            # target must be live to be chatted with (self-chat always allowed:
            # talking to your own proxy is how you audit it)
            if target_id != user_id and not target.get("profile_live"):
                raise HTTPException(status_code=404, detail="User not found")
            visibility = bool(target.get("transcript_visibility", False))
        else:
            # No users record (e.g. legacy/imported proxy): fall back to store lookup
            visibility = users.get_visibility(target_id)

        conversation_id = body.conversation_id or f"px_{uuid.uuid4().hex}"
        result = proxy.respond(
            viewer_id=user_id,
            target_id=target_id,
            conversation_id=conversation_id,
            text=body.text,
            target_visibility_on=visibility,
        )

        if result.unanswered_question is not None:
            persist_gap(result.unanswered_question)

        session = proxy.sessions.get_or_create(user_id, target_id, conversation_id)
        return ProxyOut(
            conversation_id=conversation_id,
            reply=result.reply_text,
            target_visibility_on=session.target_visibility_on,
        )

    # --- frontend (static single-page app) -------------------------------
    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(str(static_dir / "index.html"))

    return app


app = create_app()
