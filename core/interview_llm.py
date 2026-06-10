"""
LLM access for the interview (learning bot) turn.

Same pattern as core/llm.py: the engine depends on a protocol, the real client
is lazy, tests inject a fake. The interview model returns a JSON object with
`response`, `need_followup`, and `follow_up_id`.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Protocol

log = logging.getLogger("core.interview_llm")


def get_learning_model() -> str:
    return os.environ.get("LEARNING_MODEL", "gpt-5-mini")


class InterviewLLM(Protocol):
    def next_turn(
        self, *, model: str, messages: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        ...


class OpenAIInterviewLLM:
    def __init__(self, api_key_env: str = "OPENAI_API_KEY"):
        self._api_key_env = api_key_env
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI  # lazy

            api_key = os.environ.get(self._api_key_env)
            if not api_key:
                raise RuntimeError(f"{self._api_key_env} not set")
            self._client = OpenAI(api_key=api_key)
        return self._client

    def next_turn(
        self, *, model: str, messages: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        try:
            resp = self._get_client().chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or "{}"
            return json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning(f"[INTERVIEW] non-JSON reply: {e}")
            return None
        except Exception as e:
            log.error(f"[INTERVIEW] completion failed: {e}")
            return None
