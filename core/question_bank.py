"""
The question bank.

A thin, pure abstraction over the interview questions. Today it loads the same
`learning_bot/questions.json`; later you point it at the coalesced bank (your
question set + the SPC/Qualtrics items) without changing the interview engine.

Forward-compat for the coalesced bank: each question may carry optional
`type` ("free_text" | "scale" | "choice") and `feeds_spc` (bool) fields. They
are preserved and exposed but not required, so you can add them incrementally.
The interview engine only relies on `id`, `main_question`, and `followups`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


class QuestionBank:
    def __init__(self, questions: List[Dict[str, Any]]):
        self._questions = questions
        self._by_id = {q["id"]: q for q in questions}

    # --- constructors -----------------------------------------------------
    @classmethod
    def from_json(cls, path: str | Path) -> "QuestionBank":
        with open(path, "r") as f:
            return cls(json.load(f))

    # --- selection (pure; mirrors question_selector behavior) -------------
    def next_main(self, asked_main_ids: Set[str]) -> Optional[Dict[str, Any]]:
        """First main question (in file order) not yet asked, else None."""
        for q in self._questions:
            if q["id"] not in asked_main_ids:
                return q
        return None

    def followups(
        self, main_id: str, asked_followup_ids: Set[str]
    ) -> List[Dict[str, Any]]:
        """Unasked follow-ups for a given main question."""
        main = self._by_id.get(main_id)
        if not main:
            return []
        return [
            f for f in main.get("followups", [])
            if f["id"] not in asked_followup_ids
        ]

    def is_complete(self, asked_main_ids: Set[str]) -> bool:
        """True when every main question has been asked (profile-ready gate)."""
        return self.next_main(asked_main_ids) is None

    # --- introspection (useful to the onboarding UI) ----------------------
    def main_count(self) -> int:
        return len(self._questions)

    def question_type(self, qid: str) -> str:
        """'free_text' by default; structured types: likert_battery/list/long_text/choice."""
        return self._by_id.get(qid, {}).get("type", "free_text")

    def is_structured(self, qid: str) -> bool:
        return self.question_type(qid) != "free_text"

    def get(self, qid: str) -> Optional[Dict[str, Any]]:
        return self._by_id.get(qid)

    def payload(self, qid: str) -> Optional[Dict[str, Any]]:
        """UI-renderable card for a structured question (Qualtrics-style for
        likert batteries: statements x scale columns)."""
        q = self._by_id.get(qid)
        if q is None:
            return None
        p = {"question_id": q["id"], "type": q.get("type", "free_text"),
             "prompt": q["main_question"], "optional": bool(q.get("optional", False))}
        if q.get("type") == "likert_battery":
            p["scale_labels"] = q["scale_labels"]
            p["items"] = q["items"]
        elif q.get("type") == "list":
            p["min_items"] = q.get("min_items", 1)
        elif q.get("type") == "long_text":
            p["recommended_chars"] = q.get("recommended_chars", 0)
        elif q.get("type") == "choice":
            p["options"] = q["options"]
        return p

    def validate_answer(self, qid: str, answer: Any) -> Any:
        """Validate + normalize a structured answer. Raises ValueError if invalid.
        Returns the normalized answer (None for a skipped optional question)."""
        q = self._by_id.get(qid)
        if q is None:
            raise ValueError(f"Unknown question {qid}")
        qtype = q.get("type", "free_text")
        if answer is None or answer == "" or answer == []:
            if q.get("optional"):
                return None
            raise ValueError(f"{qid} requires an answer")
        if qtype == "likert_battery":
            if not isinstance(answer, dict):
                raise ValueError("Battery answer must map item_id -> 1..7")
            expected = {it["id"] for it in q["items"]}
            normalized = {}
            for item_id in expected:
                if item_id not in answer:
                    raise ValueError(f"Missing rating for {item_id}")
                try:
                    val = int(answer[item_id])
                except (TypeError, ValueError):
                    raise ValueError(f"Rating for {item_id} must be an integer")
                if not 1 <= val <= 7:
                    raise ValueError(f"Rating for {item_id} must be 1..7")
                normalized[item_id] = val
            return normalized
        if qtype == "list":
            if not isinstance(answer, list) or not all(isinstance(x, str) for x in answer):
                raise ValueError("List answer must be a list of strings")
            items = [x.strip() for x in answer if x.strip()]
            if len(items) < q.get("min_items", 1):
                raise ValueError(f"Need at least {q.get('min_items', 1)} items")
            return items
        if qtype == "long_text":
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("Answer must be non-empty text")
            return answer.strip()  # recommended_chars is advisory, not enforced
        if qtype == "choice":
            if answer not in q["options"]:
                raise ValueError(f"Answer must be one of the listed options")
            return answer
        raise ValueError(f"{qid} is not a structured question")

    def feeds_spc(self, qid: str) -> bool:
        return bool(self._by_id.get(qid, {}).get("feeds_spc", False))


def default_question_bank() -> QuestionBank:
    """Load the existing learning_bot/questions.json relative to the repo."""
    path = Path(__file__).resolve().parent.parent / "learning_bot" / "questions.json"
    return QuestionBank.from_json(path)


def question_bank_v2() -> QuestionBank:
    """The coalesced bank: base questions + SPC (TIPI, PVQ-21, context) + MBTI."""
    return QuestionBank.from_json(Path(__file__).resolve().parent / "questions_v2.json")
