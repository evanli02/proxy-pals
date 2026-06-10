"""
Schemas for the Slack-free proxy core.

These types are the contract between the proxy engine and whatever calls it
(a FastAPI route, a test, or a future proxy-to-proxy driver). Nothing here
imports Slack, MongoDB, or OpenAI -- they are plain data.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel


class ProxyResponse(BaseModel):
    """Classification + reply envelope returned by the LLM.

    Identical contract to the original proxy_bot_service.ProxyResponse so the
    OUTPUT FORMAT in proxy_bot/question_categories.py keeps working unchanged.
    """

    category: Literal[
        "identity", "preference", "experiential", "decision", "non_question"
    ]
    action: Literal["answer", "infer", "deflect", "defer"]
    has_prior_knowledge: bool
    confidence: Literal["high", "medium", "low"]
    extracted_question: Optional[str]
    response: str


@dataclass(frozen=True)
class UnansweredQuestion:
    """A question the proxy had no grounding for.

    The engine returns this as *data* instead of writing to Mongo itself, so
    the caller decides whether and where to persist it (this is what keeps the
    engine free of global side effects and safe under concurrency).
    """

    target_id: str          # whose proxy was asked (the gap belongs to them)
    asked_by: str           # viewer who asked
    conversation_id: str
    question: str
    category: str
    created_at: datetime.datetime = field(
        default_factory=datetime.datetime.utcnow
    )


@dataclass
class ReplyResult:
    """Everything produced by a single proxy turn."""

    reply_text: str
    envelope: Optional[ProxyResponse]
    # Ready-to-store assistant message (role/content/metadata), mirroring the
    # shape the original code appended to `conversation`.
    assistant_message: Dict[str, Any]
    # Present only when the proxy couldn't answer and a follow-up should be
    # queued for the target user's training. None otherwise.
    unanswered_question: Optional[UnansweredQuestion] = None
