"""
Training compiler: turn a completed interview into the proxy's grounding.

When the question bank finishes, this produces and persists everything
ProxyDefinition reads, replacing the Qualtrics webhook + CSV pipeline:

  1. TIPI/PVQ batteries -> scores (core.spc_scoring) -> a natural-language
     personality profile via the existing SPC_PROMPT_TEMPLATE (LLM, with the
     existing scores_to_natural_language fallback)
  2. loves/hates/weekday/weekend -> spc_raw.context (same shape as before)
  3. MBTI (if given) -> stored on the record and injected into the prompt
  4. QA pairs for the RAG index, from two sources:
       a. the interview transcript, via the EXISTING
          extract_qa_pairs_from_conversation (unchanged schema)
       b. the "anything else" free-text answer, decomposed by an LLM into
          atomic Q/A pairs and emitted in the SAME qa_pairs schema, so
          rag.store.upsert_qa_items embeds/upserts them identically

Everything external (LLMs, persistence) is injectable; compile_training itself
is pure-ish and fully testable offline.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .interview import InterviewState
from .question_bank import QuestionBank
from .spc_scoring import score_pvq, score_tipi

log = logging.getLogger("core.training_compiler")

ANYTHING_ELSE_DECOMPOSE_PROMPT = """\
You will receive a free-form self-description someone gave when asked \
"Anything else you want me to know about you?". Decompose it into atomic \
question/answer pairs about that person, as if someone had asked them each \
question directly and they had answered in their own words.

Rules:
- One fact or theme per pair; keep answers in the person's own words/voice \
where possible (first person).
- Questions should be natural things a new acquaintance might ask.
- Do not invent information that is not in the text.
- If the text contains nothing substantive, return an empty list.

Respond ONLY with JSON: {"pairs": [{"question": "...", "answer": "..."}, ...]}
"""


@dataclass
class CompiledTraining:
    user_id: str
    personality_text: str
    spc_raw: Dict[str, Any]              # {personality_scores, value_scores, context}
    mbti: Optional[str]
    messages: List[Dict[str, Any]]       # interview transcript (for samples)
    qa_items: List[Dict[str, Any]] = field(default_factory=list)


# --- helpers -------------------------------------------------------------------

def _find_answer_to(state: InterviewState, question_id: str) -> str:
    """The user message immediately following the assistant message that asked
    `question_id` (matches the engine's metadata convention)."""
    msgs = state.messages
    for i, m in enumerate(msgs):
        if (
            m.get("role") == "assistant"
            and (m.get("metadata") or {}).get("main_question_id") == question_id
        ):
            for follow in msgs[i + 1:]:
                if follow.get("role") == "user" and follow.get("content"):
                    return follow["content"].strip()
    return ""


def _qa_id(user_id: str, q_msg_id: str, a_msg_id: str) -> str:
    h = hashlib.sha1(f"{user_id}::{q_msg_id}::{a_msg_id}".encode()).hexdigest()[:8]
    return f"qa_{h}"


def decomposed_pairs_to_qa_items(
    user_id: str, pairs: List[Dict[str, str]], created_at: float
) -> List[Dict[str, Any]]:
    """Emit decomposed pairs in the exact qa_pairs schema used by rag.store."""
    items = []
    for i, p in enumerate(pairs):
        q = (p.get("question") or "").strip()
        a = (p.get("answer") or "").strip()
        if not q or not a:
            continue
        q_id, a_id = f"ae_q_{i}", f"ae_a_{i}"
        qa_id = _qa_id(user_id, q_id, a_id)
        items.append({
            "_id": qa_id,
            "qa_id": qa_id,
            "user_id": user_id,
            "channel_id": "web_interview",
            "q_msg_id": q_id,
            "a_msg_id": a_id,
            "question_text": q,
            "answer_text": a,
            "qa_text": f"Q: {q}\nA: {a}",
            "created_at": created_at,
        })
    return items


# --- default (real) collaborators, all lazily imported ---------------------------

def default_personality_generator(
    personality_scores: Dict[str, float], value_scores: Dict[str, float]
) -> str:
    from openai import OpenAI
    from commons.spc_pipeline import generate_personality_description

    parsed = {"personality": personality_scores, "values": value_scores, "context": {}}
    return generate_personality_description(
        parsed, OpenAI(), model=os.environ.get("SPC_MODEL", "gpt-5.4")
    )


def default_anything_else_decomposer(text: str) -> List[Dict[str, str]]:
    from openai import OpenAI

    client = OpenAI()
    try:
        resp = client.chat.completions.create(
            model=os.environ.get("LEARNING_MODEL", "gpt-5-mini"),
            messages=[
                {"role": "system", "content": ANYTHING_ELSE_DECOMPOSE_PROMPT},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        pairs = data.get("pairs", [])
        return pairs if isinstance(pairs, list) else []
    except Exception as e:
        log.error(f"[COMPILE] anything-else decomposition failed: {e}")
        return []


def default_persist(compiled: CompiledTraining) -> None:
    """Write the record + QA items, mirroring the old pipeline's shapes."""
    import datetime
    from commons.db import get_conversations_collection

    col = get_conversations_collection()
    if col is not None:
        col.update_one(
            {"user_id": compiled.user_id},
            {"$set": {
                "user_id": compiled.user_id,
                "messages": compiled.messages,
                "personality": compiled.personality_text,
                "spc_raw": compiled.spc_raw,
                "mbti": compiled.mbti,
                "spc_updated_at": datetime.datetime.utcnow(),
                "updated_at": datetime.datetime.utcnow(),
            }},
            upsert=True,
        )
    if compiled.qa_items:
        from proxy_bot.rag.store import upsert_qa_items

        upsert_qa_items(compiled.qa_items)  # embeds + upserts, unchanged path


# --- the compiler -----------------------------------------------------------------

def compile_training(
    state: InterviewState,
    bank: QuestionBank,
    *,
    personality_generator: Callable[..., str] = default_personality_generator,
    anything_else_decomposer: Callable[[str], List[Dict[str, str]]] = default_anything_else_decomposer,
    qa_extractor: Optional[Callable[[Dict[str, Any]], List[Dict[str, Any]]]] = None,
) -> CompiledTraining:
    """Pure-ish compile step. Persistence is the caller's job (default_persist)."""
    import datetime

    answers = state.structured_answers
    personality_scores = score_tipi(answers.get("SPC_TIPI") or {})
    value_scores = score_pvq(answers.get("SPC_PVQ") or {})

    context: Dict[str, str] = {}
    loves = answers.get("SPC_LOVES")
    hates = answers.get("SPC_HATES")
    if loves:
        context["loves"] = ", ".join(loves)
    if hates:
        context["hates"] = ", ".join(hates)
    if answers.get("SPC_WEEKDAY"):
        context["weekday"] = answers["SPC_WEEKDAY"]
    if answers.get("SPC_WEEKEND"):
        context["weekend"] = answers["SPC_WEEKEND"]

    mbti = answers.get("SPC_MBTI") or None

    try:
        personality_text = personality_generator(personality_scores, value_scores)
    except Exception as e:
        log.error(f"[COMPILE] personality generation failed, using fallback: {e}")
        from commons.spc_pipeline import scores_to_natural_language

        p_desc, v_desc = scores_to_natural_language(personality_scores, value_scores)
        personality_text = f"{p_desc}\n\n{v_desc}"

    created_at = datetime.datetime.utcnow().timestamp()

    # QA pairs source (a): the interview transcript, via the existing extractor
    if qa_extractor is None:
        from proxy_bot.rag.qa_extract import extract_qa_pairs_from_conversation
        qa_extractor = extract_qa_pairs_from_conversation
    conv_doc = {
        "user_id": state.user_id,
        "channel_id": "web_interview",
        "updated_at": created_at,
        "messages": state.messages,
    }
    qa_items = list(qa_extractor(conv_doc))

    # QA pairs source (b): decomposed "anything else" answers
    for qid in [q["id"] for q in getattr(bank, "_questions", []) if q.get("decompose_to_qa")]:
        text = _find_answer_to(state, qid)
        if text:
            pairs = anything_else_decomposer(text)
            extra = decomposed_pairs_to_qa_items(state.user_id, pairs, created_at)
            log.info(f"[COMPILE] decomposed {qid} into {len(extra)} QA pairs")
            qa_items.extend(extra)

    return CompiledTraining(
        user_id=state.user_id,
        personality_text=personality_text,
        spc_raw={
            "personality_scores": personality_scores,
            "value_scores": value_scores,
            "context": context,
        },
        mbti=mbti,
        messages=list(state.messages),
        qa_items=qa_items,
    )
