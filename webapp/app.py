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
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

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
    MongoKnowledgeStore,
    MongoReviewStore,
    MongoSessionStore,
    persist_unanswered_question,
)
from core.bio_suggestions import default_bio_generator
from core.explore import MongoExploreStore, rank_candidates
from webapp.auth import hash_password, hash_token, mint_token, verify_password
from webapp.social import MongoSocialStore
from webapp.users import (
    anon_profile,
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

# --------------- response models (so /docs shows real shapes) ---------------

class HealthOut(BaseModel):
    ok: bool
    version: str


class AvatarModel(BaseModel):
    """Parametric avatar. All keys optional; see API.md for legal values."""
    model_config = {"extra": "allow"}
    bg: Optional[str] = Field(default=None, description="Backdrop hex color")
    body: Optional[str] = Field(default=None, description="Body hex color")
    shape: Optional[str] = Field(default=None, description="blob|round|square|bean|egg")
    eyes: Optional[str] = None
    mouth: Optional[str] = None
    acc: Optional[str] = None
    pattern: Optional[str] = None
    blush: Optional[str] = None


class AnonProfileOut(BaseModel):
    """What strangers see: pseudonym + avatar, nothing identifying."""
    anonymous: Literal[True]
    user_id: str
    pseudonym: str
    avatar: AvatarModel
    profile_live: bool
    transcript_visibility: bool = Field(
        description="True = the owner reviews standin conversations; surface this to the user")
    you_liked: Optional[bool] = None
    likes_you: Optional[bool] = None
    connected: Optional[bool] = None
    chips: Optional[List[str]] = Field(
        default=None, description="Explore only: 0-3 'why suggested' strings")


class FullProfileOut(BaseModel):
    """Full profile: returned for yourself or a mutual connection."""
    anonymous: Literal[False]
    user_id: str
    name: str
    age: int
    bio: str
    city: str
    gender: str
    pseudonym: str
    avatar: AvatarModel
    photos: List[str] = Field(description="Photo ids; render via GET /api/photos/{id}")
    transcript_visibility: bool
    proxy_mode: Literal["strict", "mimic", "free"]
    profile_live: bool
    you_liked: Optional[bool] = None
    likes_you: Optional[bool] = None
    connected: Optional[bool] = None


class OwnProfileOut(FullProfileOut):
    """Your own profile (adds email)."""
    email: str


ProfileOut = Annotated[Union[FullProfileOut, AnonProfileOut],
                       Field(discriminator="anonymous")]


class PhotoOut(BaseModel):
    photo_id: str
    url: str


class ExploreOut(BaseModel):
    profiles: List[AnonProfileOut] = Field(
        description="Ranked best-first; entries may include chips")


class LikeOut(BaseModel):
    mutual: bool = Field(description="True = connection just formed: profiles + DMs unlocked")


class ConnectionsOut(BaseModel):
    connections: List[FullProfileOut]
    incoming: List[AnonProfileOut] = Field(
        description="People who liked you -- still anonymous until mutual")


class DmOut(BaseModel):
    model_config = {"populate_by_name": True}
    from_: str = Field(alias="from", serialization_alias="from")
    to: str
    text: str
    at: str


class DmThreadOut(BaseModel):
    messages: List[DmOut] = Field(description="Oldest first, latest 200; poll every ~4s")


class BioSuggestionsOut(BaseModel):
    suggestions: List[str] = Field(description="3-5 one-sentence bios in the user's voice")


class KnowledgeItemOut(BaseModel):
    id: str
    question: str
    answer: str
    created_at: str


class KnowledgeOut(BaseModel):
    items: List[KnowledgeItemOut]


class ReviewItemOut(BaseModel):
    id: str
    question: str
    category: str
    created_at: str


class ReviewOut(BaseModel):
    questions: List[ReviewItemOut]


class QuestionCardOut(BaseModel):
    """A structured survey card. Render by `type`; answer shapes in API.md."""
    question_id: str
    type: Literal["likert_battery", "list", "long_text", "choice"]
    prompt: str
    optional: bool = Field(description="True = null answer allowed (skip)")
    scale_labels: Optional[List[str]] = Field(default=None, description="likert_battery: 7 labels, 1..7")
    items: Optional[List[Dict[str, str]]] = Field(default=None, description="likert_battery: [{id, text}]")
    min_items: Optional[int] = Field(default=None, description="list: minimum entries")
    recommended_chars: Optional[int] = Field(default=None, description="long_text: advisory length")
    options: Optional[List[str]] = Field(default=None, description="choice: pick one")


class TranscriptMessageOut(BaseModel):
    role: Literal["user", "assistant"]
    content: str


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
    city: Optional[str] = Field(default=None, max_length=80)
    gender: Optional[str] = Field(default=None, max_length=40)
    pseudonym: Optional[str] = Field(default=None, min_length=1, max_length=40)
    avatar: Optional[Dict[str, str]] = None
    transcript_visibility: Optional[bool] = None
    proxy_mode: Optional[Literal["strict", "mimic", "free"]] = None


class DmIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class ReviewAnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)


class KnowledgeEditIn(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)


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
    question: Optional[QuestionCardOut] = None
    # status only: role/content transcript so the UI can restore on reload
    transcript: Optional[List[TranscriptMessageOut]] = None


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

    def resolve_identity(user_id: str):
        """The stand-in's speaking identity: PSEUDONYM (never the real name),
        plus shareable age/gender per the anonymity rules."""
        doc = users.get_by_id(user_id) or {}
        return {
            "display_name": doc.get("pseudonym") or "Anonymous",
            "age": doc.get("age"),
            "gender": doc.get("gender") or None,
        }

    proxy = ProxyEngine(
        definitions=ProxyDefinitionCache(resolve_name=resolve_identity),
        sessions=MongoSessionStore(),
        retriever=default_retriever,
    )

    explore_store = MongoExploreStore()

    def finalize_training(state, bank):
        """On interview completion: compile -> persist -> invalidate proxy cache
        so the freshly trained definition is what viewers get immediately."""
        compiled = compile_training(state, bank)
        default_persist(compiled)
        proxy.definitions.invalidate(state.user_id)
        try:
            explore_store.rebuild(state.user_id)   # explore features refresh
        except Exception as e:
            log.error(f"explore feature rebuild failed for {state.user_id}: {e}")

    return {
        "interview": InterviewEngine(store=MongoInterviewStore(), bank=question_bank_v2()),
        "proxy": proxy,
        "users": users,
        "persist_gap": persist_unanswered_question,
        "finalize_training": finalize_training,
        "review": MongoReviewStore(),
        "knowledge": MongoKnowledgeStore(),
        "social": MongoSocialStore(),
        "explore": explore_store,
        "bio_generator": default_bio_generator,
        "fetch_training_record": None,  # default: conversations lookup below
    }


def create_app(deps: Optional[Dict[str, Any]] = None) -> FastAPI:
    d = deps or build_deps()
    interview: InterviewEngine = d["interview"]
    proxy: ProxyEngine = d["proxy"]
    users = d["users"]
    persist_gap = d["persist_gap"]
    finalize_training = d.get("finalize_training") or (lambda state, bank: None)
    review = d.get("review")
    knowledge = d.get("knowledge")
    social = d.get("social")
    explore = d.get("explore")
    bio_generator = d.get("bio_generator") or (lambda record: [])

    def _fetch_training_record(user_id: str):
        custom = d.get("fetch_training_record")
        if custom:
            return custom(user_id)
        from commons.db import get_conversations_collection

        col = get_conversations_collection()
        if col is None:
            return None
        return col.find_one({"user_id": user_id})

    APP_VERSION = "0.4.2"  # bump on every deploy-worthy change
    app = FastAPI(title="Proxy Social Prototype API", version=APP_VERSION)

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

    @app.get("/api/health", response_model=HealthOut, tags=["Meta"], summary="Health + deployed version")
    def health():
        # version lets you verify WHICH code is actually deployed
        return {"ok": True, "version": APP_VERSION}

    # --- auth -------------------------------------------------------------------

    @app.post("/api/auth/signup", response_model=TokenOut, status_code=201, tags=["Auth"], summary="Create account, returns bearer token")
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

    @app.post("/api/auth/login", response_model=TokenOut, tags=["Auth"], summary="Login, returns bearer token")
    def login(body: LoginIn):
        doc = users.get_by_email(body.email)
        if not doc or not verify_password(body.password, doc["password_hash"]):
            # same error either way: don't reveal which emails exist
            raise HTTPException(status_code=401, detail="Invalid email or password")
        token = mint_token()
        users.save_token(hash_token(token), doc["user_id"])
        return TokenOut(token=token, user_id=doc["user_id"])

    # --- own profile ---------------------------------------------------------------

    @app.get("/api/users/me", response_model=OwnProfileOut, tags=["My profile"], summary="Own full profile")
    def me(user_id: str = Depends(get_current_user)):
        doc = users.get_by_id(user_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="User not found")
        return own_profile(doc)

    @app.patch("/api/users/me", response_model=OwnProfileOut, tags=["My profile"], summary="Edit profile fields (partial)")
    def update_me(body: ProfilePatch, user_id: str = Depends(get_current_user)):
        doc = users.update_profile(user_id, body.model_dump())
        if doc is None:
            raise HTTPException(status_code=404, detail="User not found")
        # pseudonym/age/gender feed the stand-in's identity; refresh it
        proxy.definitions.invalidate(user_id)
        return own_profile(doc)

    # --- photos ------------------------------------------------------------------------

    @app.post("/api/users/me/photos", response_model=PhotoOut, status_code=201, tags=["Photos"], summary="Upload photo (multipart, max 6)")
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

    @app.delete("/api/users/me/photos/{photo_id}", status_code=204, tags=["Photos"], summary="Delete own photo")
    def delete_photo(photo_id: str, user_id: str = Depends(get_current_user)):
        if not users.delete_photo(user_id, photo_id):
            raise HTTPException(status_code=404, detail="Photo not found")
        return Response(status_code=204)

    @app.get("/api/photos/{photo_id}", tags=["Photos"], summary="Serve photo bytes (public)")
    def get_photo(photo_id: str):
        result = users.get_photo(photo_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Photo not found")
        content, content_type = result
        return Response(content=content, media_type=content_type)

    # --- public profiles -----------------------------------------------------------------

    @app.get("/api/users", response_model=ExploreOut, tags=["Discovery"], summary="Plain live-profile list (unranked; prefer /api/explore)")
    def browse(user_id: str = Depends(get_current_user)):
        """Lo-fi discovery placeholder: live profiles, newest first.
        Real browse (filters, pagination) is Phase 3."""
        return {"profiles": users.list_live_profiles(exclude_user_id=user_id)}

    @app.get("/api/explore", response_model=ExploreOut, tags=["Discovery"], summary="Ranked anonymous suggestions with why-chips")
    def explore_feed(user_id: str = Depends(get_current_user)):
        """Ranked anonymous suggestions with why-chips. This route must never
        500: any failure degrades to the plain (or empty) list with a logged
        traceback."""
        import traceback
        try:
            cards = users.list_live_profiles(exclude_user_id=user_id)
            by_id = {c["user_id"]: c for c in cards}
        except Exception:
            log.error("explore: listing live profiles failed\n" + traceback.format_exc())
            return {"profiles": []}
        try:
            if social:
                for uid in social.connections_of(user_id):
                    by_id.pop(uid, None)
                for uid in list(by_id):
                    if social.liked(user_id, uid):
                        by_id.pop(uid, None)
            if explore is None or not by_id:
                return {"profiles": list(by_id.values())}
            viewer_feats = explore.get(user_id)
            if viewer_feats is None:
                return {"profiles": list(by_id.values())}
            cand_feats = []
            featless = []
            for uid in by_id:
                f = explore.get(uid)
                (cand_feats.append(f) if f is not None else featless.append(uid))
            likes_you = {uid for uid in by_id
                         if social and social.liked(uid, user_id)}
            ranked = rank_candidates(viewer_feats, cand_feats, likes_you=likes_you)
            out = []
            for r in ranked:
                card = dict(by_id[r["user_id"]])
                card["chips"] = r["chips"]
                out.append(card)
            out += [by_id[uid] for uid in featless]   # unranked tail, still shown
            return {"profiles": out}
        except Exception:
            # ranking is an enhancement -- browse must never break on bad or
            # legacy data. Serve the plain list and log the full traceback.
            log.error("explore ranking failed; serving unranked list\n"
                      + traceback.format_exc())
            return {"profiles": list(by_id.values())}

    @app.get("/api/users/{target_id}", response_model=ProfileOut, tags=["Discovery"], summary="Profile view: FULL if self or connected, ANONYMOUS otherwise")
    def view_profile(target_id: str, user_id: str = Depends(get_current_user)):
        doc = users.get_by_id(target_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="User not found")
        if target_id != user_id and not doc.get("profile_live"):
            # not browsable until the interview gate passes
            raise HTTPException(status_code=404, detail="User not found")
        if target_id == user_id or (social and social.connected(user_id, target_id)):
            p = public_profile(doc)          # full: owner or mutual connection
        else:
            p = anon_profile(doc)            # stranger: pseudonym + avatar only
        if social and target_id != user_id:
            p["you_liked"] = social.liked(user_id, target_id)
            p["likes_you"] = social.liked(target_id, user_id)
            p["connected"] = social.connected(user_id, target_id)
        return p

    # --- likes / connections / DMs -------------------------------------------

    @app.post("/api/likes/{target_id}", response_model=LikeOut, tags=["Social"], summary="Send a like; returns whether it became mutual")
    def send_like(target_id: str, user_id: str = Depends(get_current_user)):
        target = users.get_by_id(target_id)
        if target is None or not target.get("profile_live"):
            raise HTTPException(status_code=404, detail="User not found")
        try:
            mutual = social.like(user_id, target_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="You can't like yourself")
        return {"mutual": mutual}

    @app.get("/api/connections", response_model=ConnectionsOut, tags=["Social"], summary="Connections (full profiles) + incoming likes (anonymous)")
    def connections(user_id: str = Depends(get_current_user)):
        """Connections (full profiles) + incoming likes (anonymous)."""
        conns, incoming = [], []
        for uid in social.connections_of(user_id):
            doc = users.get_by_id(uid)
            if doc:
                conns.append(public_profile(doc))
        for uid in social.incoming_likes(user_id):
            doc = users.get_by_id(uid)
            if doc:
                a = anon_profile(doc)
                a["you_liked"] = social.liked(user_id, uid)
                incoming.append(a)
        return {"connections": conns, "incoming": incoming}

    @app.post("/api/messages/{peer_id}", response_model=DmOut, tags=["Social"], summary="Send DM (mutual connections only)")
    def send_message(peer_id: str, body: DmIn,
                     user_id: str = Depends(get_current_user)):
        if not social.connected(user_id, peer_id):
            raise HTTPException(status_code=403,
                                detail="You can only message mutual connections")
        return social.send_dm(user_id, peer_id, body.text)

    @app.get("/api/messages/{peer_id}", response_model=DmThreadOut, tags=["Social"], summary="Fetch DM thread (poll this)")
    def get_messages(peer_id: str, user_id: str = Depends(get_current_user)):
        if not social.connected(user_id, peer_id):
            raise HTTPException(status_code=403,
                                detail="You can only message mutual connections")
        return {"messages": social.get_dms(user_id, peer_id)}

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

    @app.post("/api/interview/message", response_model=InterviewOut, tags=["Training"], summary="Free-text interview turn (may return a survey card)")
    def interview_message(body: MessageIn, user_id: str = Depends(get_current_user)):
        result = interview.respond(user_id=user_id, text=body.text)
        return _interview_out(result, user_id)

    @app.post("/api/interview/skip", response_model=InterviewOut, tags=["Training"], summary="Skip the current free-text question")
    def interview_skip(user_id: str = Depends(get_current_user)):
        """Skip the current question (privacy choice). Free-text questions only."""
        result = interview.skip(user_id=user_id)
        return _interview_out(result, user_id)

    @app.post("/api/interview/restart", status_code=204, tags=["Training"], summary="Retrain from scratch (old standin stays live meanwhile)")
    def interview_restart(user_id: str = Depends(get_current_user)):
        """Retrain from scratch. The existing proxy stays live (old training)
        until the new interview completes and recompiles."""
        interview.store.reset(user_id)
        return Response(status_code=204)

    @app.post("/api/users/me/bio-suggestions", response_model=BioSuggestionsOut, tags=["My profile"], summary="3-5 bio suggestions from training data")
    def bio_suggestions(user_id: str = Depends(get_current_user)):
        record = _fetch_training_record(user_id)
        if not record:
            raise HTTPException(status_code=409,
                                detail="Finish training your standin first")
        return {"suggestions": bio_generator(record)}

    @app.get("/api/knowledge", response_model=KnowledgeOut, tags=["Standin knowledge"], summary="Everything your standin can answer")
    def knowledge_list(user_id: str = Depends(get_current_user)):
        """Everything your standin can answer: question/answer pairs."""
        if knowledge is None:
            return {"items": []}
        return {"items": knowledge.list(user_id)}

    @app.patch("/api/knowledge/{qa_id}", status_code=204, tags=["Standin knowledge"], summary="Edit an answer (re-embeds immediately)")
    def knowledge_edit(qa_id: str, body: KnowledgeEditIn,
                       user_id: str = Depends(get_current_user)):
        if knowledge is None or not knowledge.update(user_id, qa_id, body.answer):
            raise HTTPException(status_code=404, detail="Not found")
        return Response(status_code=204)

    @app.delete("/api/knowledge/{qa_id}", status_code=204, tags=["Standin knowledge"], summary="Delete a fact")
    def knowledge_delete(qa_id: str, user_id: str = Depends(get_current_user)):
        if knowledge is None or not knowledge.delete(user_id, qa_id):
            raise HTTPException(status_code=404, detail="Not found")
        return Response(status_code=204)

    @app.get("/api/review", response_model=ReviewOut, tags=["Standin knowledge"], summary="Questions your standin could not answer")
    def review_pending(user_id: str = Depends(get_current_user)):
        """Questions people asked your standin that it couldn't answer."""
        if review is None:
            return {"questions": []}
        return {"questions": review.pending(user_id)}

    @app.post("/api/review/{item_id}/answer", status_code=204, tags=["Standin knowledge"], summary="Teach an answer to a gap")
    def review_answer(item_id: str, body: ReviewAnswerIn,
                      user_id: str = Depends(get_current_user)):
        """Answer a gap: it becomes searchable knowledge for your standin."""
        if review is None or not review.answer(user_id, item_id, body.answer):
            raise HTTPException(status_code=404, detail="Question not found")
        proxy.definitions.invalidate(user_id)
        return Response(status_code=204)

    @app.post("/api/interview/answer", response_model=InterviewOut, tags=["Training"], summary="Submit a structured survey answer")
    def interview_answer(body: StructuredAnswerIn, user_id: str = Depends(get_current_user)):
        """Submit a structured answer (likert battery / list / long_text / choice)."""
        try:
            result = interview.submit_answer(
                user_id=user_id, question_id=body.question_id, answer=body.answer
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return _interview_out(result, user_id)

    @app.get("/api/interview/status", response_model=InterviewOut, tags=["Training"], summary="Progress + transcript + pending card (restore on launch)")
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

    @app.post("/api/proxy/{target_id}/message", response_model=ProxyOut, tags=["Standin chat"], summary="Talk to a standin (echo conversation_id to continue)")
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
            mode = target.get("proxy_mode", "mimic")
        else:
            # No users record (e.g. legacy/imported proxy): fall back to store lookup
            visibility = users.get_visibility(target_id)
            mode = "mimic"

        conversation_id = body.conversation_id or f"px_{uuid.uuid4().hex}"
        result = proxy.respond(
            viewer_id=user_id,
            target_id=target_id,
            conversation_id=conversation_id,
            text=body.text,
            target_visibility_on=visibility,
            mode=mode if mode in ("strict", "mimic", "free") else "mimic",
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
            # no-cache so deploys take effect immediately; the versioned
            # ?v=N asset URLs inside handle caching of js/css correctly
            return FileResponse(str(static_dir / "index.html"),
                                headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
