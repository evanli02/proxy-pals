"""
LLM access for the proxy core.

The engine depends on the ``ProxyLLM`` protocol, not on OpenAI directly, so:
  - tests inject a fake and never hit the network,
  - the real client is constructed lazily (importing the core needs no API key
    and makes no network call -- unlike the old module that called
    Slack ``auth_test()`` at import time).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Protocol

from .schemas import ProxyResponse

log = logging.getLogger("core.llm")


def get_proxy_model() -> str:
    return os.environ.get("PROXY_MODEL", "gpt-5.4")


class ProxyLLM(Protocol):
    """Anything that can turn a message stack into a ProxyResponse envelope."""

    def classify_and_reply(
        self, *, model: str, messages: List[Dict[str, Any]]
    ) -> Optional[ProxyResponse]:
        ...


class OpenAIProxyLLM:
    """Real implementation backed by OpenAI's structured-output parse.

    The OpenAI client is created on first use, not at import, so the core
    stays importable in any environment.
    """

    def __init__(self, api_key_env: str = "OPENAI_API_KEY", effort: str = "medium"):
        self._api_key_env = api_key_env
        self._effort = effort
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI  # lazy import

            api_key = os.environ.get(self._api_key_env)
            if not api_key:
                raise RuntimeError(f"{self._api_key_env} not set")
            self._client = OpenAI(api_key=api_key)
        return self._client

    def classify_and_reply(
        self, *, model: str, messages: List[Dict[str, Any]]
    ) -> Optional[ProxyResponse]:
        try:
            resp = self._get_client().responses.parse(
                model=model,
                input=messages,
                reasoning={"effort": self._effort},
                text_format=ProxyResponse,
            )
            return resp.output_parsed
        except Exception as e:  # never raise into the engine; fall back safely
            log.error(f"[CLASSIFICATION] responses.parse failed: {e}")
            return None
