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

from .interview_prompt import get_interview_prompt, get_topic_prompt

from .interview_llm import InterviewLLM, OpenAIInterviewLLM, get_learning_model
from .question_bank import QuestionBank, default_question_bank

# Topic conversations: each chosen topic gets an opening ask plus 3-5 dynamic
# follow-ups (the LLM may wrap up after the minimum; it must stop at the max).
TOPIC_MIN_FOLLOWUPS = 3
TOPIC_MAX_FOLLOWUPS = 5


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
    # topic-conversation mode: set while a chosen topic is being discussed
    active_topic_id: str = ""
    active_topic: str = ""
    topic_followup_count: int = 0
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
_SURVEY_BRIDGE_MESSAGE = (
    "(The user just finished a section of survey-style questions -- there is "
    "no chat message from them to react to. Welcome them back to the "
    "conversation in a short, warm sentence and ask the next question.)"
)


def run_interview_turn(
    state: InterviewState,
    user_message: str,
    bank: QuestionBank,
    llm: InterviewLLM,
    model: str,
    skip: bool = False,
    append_user: bool = True,
) -> InterviewTurnResult:
    """``append_user=False`` runs an interviewer-initiated turn (e.g. the
    bridge after a survey section): no user message is recorded; the synthetic
    ``user_message`` only steers the prompt."""
    if skip:
        state.messages.append({
            "role": "user", "content": SKIP_MARKER, "metadata": {"skipped": True},
        })
        user_message = _SKIP_PROMPT_MESSAGE
    elif append_user:
        state.messages.append({"role": "user", "content": user_message})

    # Mid-topic conversation: the topic loop owns the turn (its own follow-up
    # budget and prompt) until the topic wraps and the flow advances.
    if state.active_topic_id:
        return _run_topic_turn(state, user_message, bank, llm, model, skip=skip)

    asked = set(state.asked_ids)
    prev_q = state.previous_question or ""
    prev_qid = state.previous_question_id or ""

    # dynamic follow-ups: at most ONE per main question, crafted by the LLM
    # to fit the user's actual answer (no predefined list). Not allowed on a
    # skip, when there's no previous main, or when the previous question
    # disallows them (identity intake, STRUCTURED survey items, topic cards --
    # follow-ups are for conversation, not for probing someone's Likert
    # ratings, routine write-ups, or their name).
    followup_allowed = (
        bool(prev_qid)
        and bank.allows_followup(prev_qid)
        and len(state.follow_up_ids) == 0
        and not skip
    )
    next_main = bank.next_main(asked)

    if next_main is None:
        # All main questions asked -> interview complete, profile ready.
        return InterviewTurnResult(
            reply_text=None, complete=True, profile_ready=True, assistant_message=None
        )

    if bank.asks_verbatim(next_main["id"]):
        # Identity intake: a static question asked word-for-word, no LLM, no
        # follow-ups, identical for every user.
        state.previous_question = next_main["main_question"]
        state.previous_question_id = next_main["id"]
        state.asked_ids.append(next_main["id"])
        state.follow_up_ids = []
        assistant_message = {
            "role": "assistant",
            "content": next_main["main_question"],
            "metadata": {"need_followup": False,
                         "main_question_id": next_main["id"],
                         "identity": True},
        }
        state.messages.append(assistant_message)
        return InterviewTurnResult(
            reply_text=next_main["main_question"], complete=False,
            profile_ready=False, assistant_message=assistant_message,
        )

    if bank.is_structured(next_main["id"]) or bank.is_topic(next_main["id"]):
        # A survey section is next -- but the answer the user JUST gave (to a
        # free-text question) still deserves its one follow-up chance. Ask the
        # LLM with no next-main on offer: either it crafts a tailored
        # follow-up, or we proceed straight to the survey card.
        if followup_allowed:
            system = get_interview_prompt(None, True, prev_q, user_message)
            messages = [{"role": "system", "content": system}] + [
                {"role": m["role"], "content": m["content"]} for m in state.messages
            ]
            parsed = llm.next_turn(model=model, messages=messages) or {}
            fu_text = (parsed.get("response") or "").strip()
            if parsed.get("need_followup") and fu_text:
                state.follow_up_ids.append("dynamic")
                assistant_message = {
                    "role": "assistant",
                    "content": fu_text,
                    "metadata": {"need_followup": True,
                                 "main_question_id": prev_qid},
                }
                state.messages.append(assistant_message)
                return InterviewTurnResult(
                    reply_text=fu_text, complete=False, profile_ready=False,
                    assistant_message=assistant_message,
                )
        # Structured/topic questions bypass the LLM entirely: the UI renders
        # the card (Qualtrics-style matrix for batteries, topic picker for
        # topics) and answers come back through submit_structured_answer /
        # submit_topic_choice. This is what keeps validated scale items
        # scorable instead of dissolving into free text.
        return _issue_structured(state, next_main, bank)

    system = get_interview_prompt(
        next_main["main_question"], followup_allowed, prev_q, user_message
    )
    messages = [{"role": "system", "content": system}] + [
        {"role": m["role"], "content": m["content"]} for m in state.messages
    ]

    parsed = llm.next_turn(model=model, messages=messages) or {}
    reply_text = parsed.get("response", "") or ""
    need_followup = bool(parsed.get("need_followup", False)) and followup_allowed

    if need_followup:
        state.follow_up_ids.append("dynamic")             # one per main, spent
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
    state: InterviewState, q: Dict[str, Any], bank: QuestionBank,
    reply_text: Optional[str] = None,
) -> InterviewTurnResult:
    """Return the structured/topic card; idempotent if it's already pending.
    ``reply_text`` optionally carries a chat remark (e.g. a topic wrap-up) to
    show BEFORE the card -- the card renders its own prompt, so the reply is
    never the prompt itself."""
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
        reply_text=reply_text,
        complete=False,
        profile_ready=False,
        assistant_message=None,
        question_payload=payload,
    )


def _run_topic_turn(
    state: InterviewState,
    user_message: str,
    bank: QuestionBank,
    llm: InterviewLLM,
    model: str,
    skip: bool = False,
) -> InterviewTurnResult:
    """One turn inside an active topic conversation. The user message is
    already appended. Follow-ups continue until the LLM wraps up (after the
    minimum) or the budget runs out; then the flow advances past the topic."""
    if skip:
        # skipping mid-topic ends the topic early, no LLM wrap-up needed
        return _end_topic(state, bank, llm, model, wrap_text=None)

    count = state.topic_followup_count
    must = count < TOPIC_MIN_FOLLOWUPS
    may = count < TOPIC_MAX_FOLLOWUPS

    system = get_topic_prompt(
        state.active_topic, phase="conversation",
        must_followup=must, may_followup=may, user_message=user_message,
    )
    messages = [{"role": "system", "content": system}] + [
        {"role": m["role"], "content": m["content"]} for m in state.messages
    ]
    parsed = llm.next_turn(model=model, messages=messages) or {}
    reply_text = (parsed.get("response") or "").strip()
    need_followup = bool(parsed.get("need_followup", False))

    if may and (need_followup or must) and reply_text:
        state.topic_followup_count += 1
        assistant_message = {
            "role": "assistant",
            "content": reply_text,
            "metadata": {"need_followup": True,
                         "main_question_id": state.active_topic_id,
                         "topic_followup": True},
        }
        state.messages.append(assistant_message)
        return InterviewTurnResult(
            reply_text=reply_text, complete=False, profile_ready=False,
            assistant_message=assistant_message,
        )

    # Wrap up. Guard against the model trying to keep going after the budget
    # is spent (need_followup=True with no budget left) or sneaking a question
    # into its closing remark: the next card appears immediately after this
    # turn, so a question here would be stranded unanswered. Drop any
    # question-shaped reply instead of showing it.
    if need_followup or "?" in reply_text:
        reply_text = ""
    if reply_text:
        state.messages.append({
            "role": "assistant",
            "content": reply_text,
            "metadata": {"main_question_id": state.active_topic_id,
                         "topic_wrap": True},
        })
    return _end_topic(state, bank, llm, model, wrap_text=reply_text or None)


def _end_topic(
    state: InterviewState,
    bank: QuestionBank,
    llm: InterviewLLM,
    model: str,
    wrap_text: Optional[str],
) -> InterviewTurnResult:
    """Close the active topic and advance to whatever the bank has next."""
    qid = state.active_topic_id
    state.previous_question = state.active_topic
    state.previous_question_id = qid
    state.asked_ids.append(qid)
    state.follow_up_ids = []
    state.active_topic_id = ""
    state.active_topic = ""
    state.topic_followup_count = 0

    nxt = bank.next_main(set(state.asked_ids))
    if nxt is None:
        return InterviewTurnResult(
            reply_text=wrap_text, complete=True, profile_ready=True,
            assistant_message=None,
        )
    if bank.is_structured(nxt["id"]) or bank.is_topic(nxt["id"]):
        return _issue_structured(state, nxt, bank, reply_text=wrap_text)
    # a conversational question is next: bridge straight into it so the chat
    # never goes silent (same rationale as the survey bridge)
    return run_interview_turn(
        state, _SURVEY_BRIDGE_MESSAGE, bank, llm, model, append_user=False,
    )


def submit_topic_choice(
    state: InterviewState,
    question_id: str,
    topic: Any,
    bank: QuestionBank,
    llm: InterviewLLM,
    model: str,
) -> InterviewTurnResult:
    """Record the user's chosen topic (preset or self-written) and open the
    topic conversation with an LLM-crafted first question. Raises ValueError
    on invalid or out-of-order submissions."""
    next_main = bank.next_main(set(state.asked_ids))
    if next_main is None:
        return InterviewTurnResult(
            reply_text=None, complete=True, profile_ready=True, assistant_message=None
        )
    if question_id != next_main["id"] or not bank.is_topic(question_id):
        raise ValueError(f"Expected a topic choice for {next_main['id']}, got {question_id}")
    topic = (str(topic or "")).strip()
    if not topic:
        raise ValueError("Pick a topic or write your own")
    topic = topic[:200]

    state.structured_answers[question_id] = topic
    state.pending_structured_id = ""
    state.messages.append({
        "role": "user",
        "content": topic,
        "metadata": {"topic_choice_for": question_id},
    })
    state.active_topic_id = question_id
    state.active_topic = topic
    state.topic_followup_count = 0

    system = get_topic_prompt(topic, phase="opening")
    messages = [{"role": "system", "content": system}] + [
        {"role": m["role"], "content": m["content"]} for m in state.messages
    ]
    parsed = llm.next_turn(model=model, messages=messages) or {}
    # if the LLM fails, fall back to asking the chosen topic verbatim
    reply_text = (parsed.get("response") or "").strip() or topic
    assistant_message = {
        "role": "assistant",
        "content": reply_text,
        "metadata": {"main_question_id": question_id, "topic_opening": True},
    }
    state.messages.append(assistant_message)
    return InterviewTurnResult(
        reply_text=reply_text, complete=False, profile_ready=False,
        assistant_message=assistant_message,
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

    def choose_topic(self, *, user_id: str, question_id: str, topic: Any) -> InterviewTurnResult:
        """The user picked (or wrote) a topic on a topic_choice card."""
        state = self.store.get_or_create(user_id)
        with state.lock:
            result = submit_topic_choice(
                state, question_id, topic, self.bank, self.llm, self.model
            )
            self.store.save(state, profile_ready=result.profile_ready)
            return result

    def submit_answer(self, *, user_id: str, question_id: str, answer: Any) -> InterviewTurnResult:
        state = self.store.get_or_create(user_id)
        with state.lock:
            result = submit_structured_answer(state, question_id, answer, self.bank)
            if not result.complete and result.question_payload is None:
                # BRIDGE TURN: the next question is conversational, and the
                # engine otherwise only speaks in response to a user message --
                # without this, the chat re-enables in silence and the next
                # question is never asked until the user speaks first.
                result = run_interview_turn(
                    state, _SURVEY_BRIDGE_MESSAGE, self.bank, self.llm,
                    self.model, append_user=False,
                )
            self.store.save(state, profile_ready=result.profile_ready)
            return result
