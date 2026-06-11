"""
The interview (learning bot) engine.

`run_interview_turn` is a pure function that faithfully reproduces the state
machine from `learning_bot_service._handle_message_internal`, minus Slack and
minus the module globals:

  - append the user message
  - look up unasked follow-ups for the last main question + the next unasked
    main question
  - if no unasked main remains -> the interview is complete (profile ready)
  - otherwise build the prompt, ask the LLM, then EITHER record a follow-up
    (stay on the same main) OR advance to the next main and clear follow-ups

The onboarding greeting is intentionally dropped here: in the web app the UI
shows the greeting when the interview screen opens, so the engine never
prepends it.

`profile_ready` flips true once every main question has been asked -- the gate
you described ("when the question bank is complete, the profile is ready").
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .interview_prompt import get_interview_prompt

from .interview_llm import InterviewLLM, OpenAIInterviewLLM, get_learning_model
from .question_bank import QuestionBank, default_question_bank


@dataclass
class InterviewState:
    user_id: str
    asked_ids: List[str] = field(default_factory=list)
    follow_up_ids: List[str] = field(default_factory=list)
    previous_question: str = ""
    previous_question_id: str = ""
    pending_structured_id: str = ""
    structured_answers: Dict[str, Any] = field(default_factory=dict)
    messages: List[Dict[str, Any]] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


@dataclass
class InterviewTurnResult:
    reply_text: Optional[str]
    complete: bool          # the interview finished (no main questions left)
    profile_ready: bool     # the bank is fully covered -> proxy can go live
    assistant_message: Optional[Dict[str, Any]]
    # Present when the next question is structured (likert battery / list /
    # long_text / choice): the UI renders this card instead of free chat.
    question_payload: Optional[Dict[str, Any]] = None


SKIP_MARKER = "(skipped)"
_SKIP_PROMPT_MESSAGE = (
    "(The user tapped Skip -- they'd rather not answer that one. Acknowledge "
    "warmly in a word or two WITHOUT prying or commenting on the skip, and "
    "move straight on to the next question.)"
)


def run_interview_turn(
    state: InterviewState,
    user_message: str,
    bank: QuestionBank,
    llm: InterviewLLM,
    model: str,
    skip: bool = False,
) -> InterviewTurnResult:
    if skip:
        state.messages.append({
            "role": "user", "content": SKIP_MARKER, "metadata": {"skipped": True},
        })
        user_message = _SKIP_PROMPT_MESSAGE
    else:
        state.messages.append({"role": "user", "content": user_message})

    asked = set(state.asked_ids)
    fu_asked = set(state.follow_up_ids)
    prev_q = state.previous_question or ""
    prev_qid = state.previous_question_id or ""

    followups = bank.followups(prev_qid, fu_asked) if prev_qid else []
    next_main = bank.next_main(asked)

    if next_main is None:
        # All main questions asked -> interview complete, profile ready.
        return InterviewTurnResult(
            reply_text=None, complete=True, profile_ready=True, assistant_message=None
        )

    if bank.is_structured(next_main["id"]):
        # Structured questions bypass the LLM entirely: the UI renders the
        # card (Qualtrics-style matrix for batteries) and answers come back
        # through submit_structured_answer. This is what keeps validated
        # scale items scorable instead of dissolving into free text.
        return _issue_structured(state, next_main, bank)

    system = get_interview_prompt(
        next_main["main_question"], followups, prev_q, user_message
    )
    messages = [{"role": "system", "content": system}] + [
        {"role": m["role"], "content": m["content"]} for m in state.messages
    ]

    parsed = llm.next_turn(model=model, messages=messages) or {}
    reply_text = parsed.get("response", "") or ""
    need_followup = bool(parsed.get("need_followup", False))
    follow_up_id = parsed.get("follow_up_id") or ""

    if need_followup and follow_up_id:
        state.follow_up_ids.append(follow_up_id)          # stay on same main
    else:
        state.previous_question = next_main["main_question"]
        state.previous_question_id = next_main["id"]
        state.asked_ids.append(next_main["id"])           # advance main
        state.follow_up_ids = []                           # clear_follow_ups

    assistant_message = {
        "role": "assistant",
        "content": reply_text,
        "metadata": {
            "need_followup": need_followup,
            "follow_up_id": follow_up_id or None,
            "main_question_id": next_main["id"],
        },
    }
    state.messages.append(assistant_message)

    # NOTE: asking the final question does NOT complete the interview --
    # completion happens on the NEXT turn, when the user's answer to it
    # arrives and next_main comes back None. (profile_ready == answered-all,
    # never asked-all; flipping it here was the skipped-last-question bug.)
    return InterviewTurnResult(
        reply_text=reply_text,
        complete=False,
        profile_ready=False,
        assistant_message=assistant_message,
    )


def _issue_structured(
    state: InterviewState, q: Dict[str, Any], bank: QuestionBank
) -> InterviewTurnResult:
    """Return the structured card; idempotent if it's already pending."""
    payload = bank.payload(q["id"])
    if state.pending_structured_id != q["id"]:
        state.pending_structured_id = q["id"]
        state.messages.append({
            "role": "assistant",
            "content": q["main_question"],
            "metadata": {"main_question_id": q["id"], "structured": True,
                         "type": q.get("type")},
        })
    return InterviewTurnResult(
        reply_text=q["main_question"],
        complete=False,
        profile_ready=False,
        assistant_message=None,
        question_payload=payload,
    )


def _answer_to_transcript(answer: Any) -> str:
    if answer is None:
        return "(skipped)"
    if isinstance(answer, dict):
        return "; ".join(f"{k}={v}" for k, v in sorted(answer.items()))
    if isinstance(answer, list):
        return ", ".join(answer)
    return str(answer)


def submit_structured_answer(
    state: InterviewState,
    question_id: str,
    answer: Any,
    bank: QuestionBank,
) -> InterviewTurnResult:
    """Record a structured answer and advance. Raises ValueError on invalid
    answers or out-of-order submissions."""
    asked = set(state.asked_ids)
    next_main = bank.next_main(asked)
    if next_main is None:
        return InterviewTurnResult(
            reply_text=None, complete=True, profile_ready=True, assistant_message=None
        )
    if question_id != next_main["id"] or not bank.is_structured(question_id):
        raise ValueError(f"Expected answer for {next_main['id']}, got {question_id}")

    normalized = bank.validate_answer(question_id, answer)  # may raise
    state.structured_answers[question_id] = normalized
    state.messages.append({
        "role": "user",
        "content": _answer_to_transcript(normalized),
        "metadata": {"structured_answer_for": question_id},
    })
    state.previous_question = next_main["main_question"]
    state.previous_question_id = question_id
    state.asked_ids.append(question_id)
    state.follow_up_ids = []
    state.pending_structured_id = ""

    # If the next question is also structured, issue its card immediately so
    # batteries chain without an empty chat turn in between.
    following = bank.next_main(set(state.asked_ids))
    if following is not None and bank.is_structured(following["id"]):
        return _issue_structured(state, following, bank)

    profile_ready = bank.is_complete(set(state.asked_ids))
    return InterviewTurnResult(
        reply_text=None,
        complete=profile_ready,
        profile_ready=profile_ready,
        assistant_message=None,
    )


class InMemoryInterviewStore:
    """One interview per user_id. (Swap for a Mongo-backed store later.)"""

    def __init__(self):
        self._states: Dict[str, InterviewState] = {}
        self._lock = threading.RLock()

    def get_or_create(self, user_id: str) -> InterviewState:
        with self._lock:
            state = self._states.get(user_id)
            if state is None:
                state = InterviewState(user_id=user_id)
                self._states[user_id] = state
            return state

    def save(self, state: InterviewState, profile_ready: bool = False) -> None:
        return None

    def reset(self, user_id: str) -> None:
        with self._lock:
            self._states.pop(user_id, None)


class InterviewEngine:
    """One call for the onboarding screen: 'user U said X to the interviewer'."""

    def __init__(
        self,
        bank: Optional[QuestionBank] = None,
        store: Optional[InMemoryInterviewStore] = None,
        llm: Optional[InterviewLLM] = None,
        model: Optional[str] = None,
    ):
        self.bank = bank or default_question_bank()
        self.store = store or InMemoryInterviewStore()
        self.llm = llm or OpenAIInterviewLLM()
        self.model = model or get_learning_model()

    def respond(self, *, user_id: str, text: str) -> InterviewTurnResult:
        state = self.store.get_or_create(user_id)
        with state.lock:
            result = run_interview_turn(state, text, self.bank, self.llm, self.model)
            self.store.save(state, profile_ready=result.profile_ready)
            return result

    def skip(self, *, user_id: str) -> InterviewTurnResult:
        """Skip the current free-text question (privacy choice)."""
        state = self.store.get_or_create(user_id)
        with state.lock:
            result = run_interview_turn(state, "", self.bank, self.llm, self.model, skip=True)
            self.store.save(state, profile_ready=result.profile_ready)
            return result

    def submit_answer(self, *, user_id: str, question_id: str, answer: Any) -> InterviewTurnResult:
        state = self.store.get_or_create(user_id)
        with state.lock:
            result = submit_structured_answer(state, question_id, answer, self.bank)
            self.store.save(state, profile_ready=result.profile_ready)
            return result
