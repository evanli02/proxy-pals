"""
The proxy engine.

``generate_reply`` is a pure function: definition + history + message in,
``ReplyResult`` out. It performs no Slack calls, no DB writes, and touches no
global state -- the unanswered-question gap is *returned as data* for the caller
to persist if it wants. This is the direct replacement for the Slack-bound
``forward_to_openai_and_reply``.

``ProxyEngine`` wires a definition cache, a session store, and an LLM together
into the one call a web route makes. Concurrency safety comes for free: the
definition is shared read-only, the session is per-conversation locked.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from .llm import OpenAIProxyLLM, ProxyLLM, get_proxy_model
from .proxy_definition import ProxyDefinition, ProxyDefinitionCache
from .schemas import ProxyResponse, ReplyResult, UnansweredQuestion
from .sessions import InMemorySessionStore, ProxySession, SessionStore

log = logging.getLogger("core.proxy_engine")

_FALLBACK_TEXT = "(no response)"


def generate_reply(
    definition: ProxyDefinition,
    history: List[Dict[str, Any]],
    user_message: str,
    llm: ProxyLLM,
    model: str,
    *,
    viewer_id: str = "",
    conversation_id: str = "",
    retrieved_qa: Optional[List[Dict[str, Any]]] = None,
) -> ReplyResult:
    """Produce one proxy turn. Pure: no I/O beyond the injected ``llm`` call.

    ``retrieved_qa`` is RAG context fetched by the caller (top-k qa_pairs for
    the target); it's injected as a system block so grounded answers can draw
    on training facts beyond the prompt's sample window.
    """
    system_blocks = [{"role": "system", "content": definition.system_prompt()}]
    if retrieved_qa:
        context = "\n---\n".join(
            it.get("qa_text", "") for it in retrieved_qa[:5] if it.get("qa_text")
        )
        if context:
            system_blocks.append({
                "role": "system",
                "content": (
                    "[RELEVANT THINGS YOU'VE SHARED BEFORE] - prior Q&A from "
                    "your training; use only what is strictly relevant:\n" + context
                ),
            })
    messages = system_blocks + history + [{"role": "user", "content": user_message}]

    envelope: Optional[ProxyResponse] = llm.classify_and_reply(model=model, messages=messages)

    if envelope is None:
        log.warning("[CLASSIFICATION] no parsed response; safe fallback")
        reply_text, category, action = _FALLBACK_TEXT, "unknown", "unknown"
        has_prior_knowledge, confidence, extracted_question = False, "low", None
    else:
        reply_text = (envelope.response or "").strip() or _FALLBACK_TEXT
        category = envelope.category
        action = envelope.action
        has_prior_knowledge = envelope.has_prior_knowledge
        confidence = envelope.confidence
        extracted_question = envelope.extracted_question

    log.info(
        f"[CLASSIFICATION] target={definition.user_id} category={category} "
        f"action={action} prior={has_prior_knowledge} conf={confidence} "
        f"q={extracted_question} msg={user_message[:80]!r}"
    )

    gap: Optional[UnansweredQuestion] = None
    if (
        not has_prior_knowledge
        and category not in ("non_question", "unknown")
        and extracted_question
    ):
        gap = UnansweredQuestion(
            target_id=definition.user_id,
            asked_by=viewer_id,
            conversation_id=conversation_id,
            question=extracted_question,
            category=category,
        )

    assistant_message = {
        "role": "assistant",
        "content": reply_text,
        "metadata": {
            "category": category,
            "action": action,
            "has_prior_knowledge": has_prior_knowledge,
            "confidence": confidence,
            "extracted_question": extracted_question,
            "user_query": user_message,
        },
    }

    return ReplyResult(
        reply_text=reply_text,
        envelope=envelope,
        assistant_message=assistant_message,
        unanswered_question=gap,
    )


def default_retriever(user_id: str, query: str, k: int = 3) -> List[Dict[str, Any]]:
    """Atlas vector search over qa_pairs (lazy import; safe failure -> [])."""
    try:
        from proxy_bot.rag.search import search_similar_qa

        return search_similar_qa(user_id, query, k=k)
    except Exception as e:
        log.warning(f"[RAG] retrieval failed for {user_id}: {e}")
        return []


# (viewer asks something) -> retriever(target_id, text) -> qa_text snippets
Retriever = Callable[[str, str], List[Dict[str, Any]]]


class ProxyEngine:
    """One call for a web route: 'viewer V says X to target T's proxy'."""

    def __init__(
        self,
        definitions: Optional[ProxyDefinitionCache] = None,
        sessions: Optional[SessionStore] = None,
        llm: Optional[ProxyLLM] = None,
        model: Optional[str] = None,
        retriever: Optional[Retriever] = None,
    ):
        self.definitions = definitions or ProxyDefinitionCache()
        self.sessions = sessions or InMemorySessionStore()
        self.llm = llm or OpenAIProxyLLM()
        self.model = model or get_proxy_model()
        # None -> no retrieval (tests); use default_retriever for production
        self.retriever = retriever

    def respond(
        self,
        *,
        viewer_id: str,
        target_id: str,
        conversation_id: str,
        text: str,
        target_visibility_on: bool = False,
    ) -> ReplyResult:
        definition = self.definitions.get(target_id)  # shared, read-only
        session = self.sessions.get_or_create(
            viewer_id, target_id, conversation_id, target_visibility_on
        )

        session.append({"role": "user", "content": text})
        history = session.history_snapshot()
        # Drop the just-appended user turn from history; generate_reply re-adds it.
        history = history[:-1]

        retrieved = self.retriever(target_id, text) if self.retriever else None
        result = generate_reply(
            definition,
            history,
            text,
            self.llm,
            self.model,
            viewer_id=viewer_id,
            conversation_id=conversation_id,
            retrieved_qa=retrieved,
        )

        session.append(result.assistant_message)
        self.sessions.save(session)
        return result
