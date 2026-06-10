"""
System-prompt construction for the proxy.

This is a faithful port of ``proxy_bot_service.build_system_prompt`` -- it was
already a pure function, so it moves here unchanged in behavior. The long
prompt-text constants are imported from the existing modules so there is a
single source of truth (no duplication / drift).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from proxy_bot.proxy_bot_prompts import PROXY_BOT_PROMPT
from proxy_bot.proxy_bot_mimic_prompts import (
    PROXY_BOT_MIMIC_PROMPT,
    PROXY_BOT_FREE_PROMPT,
)
from proxy_bot.question_categories import (
    MIMIC_CLASSIFICATION_PROMPT,
    STRICT_CLASSIFICATION_PROMPT,
    FREE_CLASSIFICATION_PROMPT,
)

_STYLE_KEYS = [
    "summary", "formality", "cadence", "punctuation", "emoji_and_markers",
    "lexicon", "style_rules_do", "style_rules_dont", "safety_note",
]


def rules_prompt_for(mode: str) -> str:
    if mode == "free":
        return PROXY_BOT_FREE_PROMPT
    if mode == "mimic":
        return PROXY_BOT_MIMIC_PROMPT
    return PROXY_BOT_PROMPT


def _classification_prompt_for(mode: str) -> str:
    if mode == "free":
        return FREE_CLASSIFICATION_PROMPT
    if mode == "mimic":
        return MIMIC_CLASSIFICATION_PROMPT
    return STRICT_CLASSIFICATION_PROMPT


def build_system_prompt(
    partner_name: str,
    style_rules: Optional[Dict[str, Any]],
    sample_messages: List[str],
    personality_details: str = "",
    spc_context: Optional[Dict[str, str]] = None,
    mode: str = "mimic",
    mbti: Optional[str] = None,
) -> str:
    """Create a system prompt instructing the model to be ``partner_name``."""
    style_snippet = ""
    if style_rules:
        condensed = {k: style_rules.get(k) for k in _STYLE_KEYS if k in style_rules}
        try:
            style_snippet = json.dumps(condensed, ensure_ascii=False)
        except Exception:
            style_snippet = str(condensed)

    samples_text = "\n- ".join(sample_messages) if sample_messages else ""

    context_section = ""
    if spc_context:
        parts = []
        if spc_context.get("loves"):
            parts.append(f"Things you love: {spc_context['loves']}")
        if spc_context.get("hates"):
            parts.append(f"Things you hate: {spc_context['hates']}")
        if spc_context.get("weekday"):
            parts.append(f"Your typical weekday: {spc_context['weekday']}")
        if spc_context.get("weekend"):
            parts.append(f"Your typical weekend: {spc_context['weekend']}")
        if parts:
            context_section = "YOUR PERSONAL LIFE CONTEXT:\n" + "\n".join(parts) + "\n\n"

    rules_prompt = rules_prompt_for(mode)
    classification_prompt = _classification_prompt_for(mode)

    personality_section = f"YOUR PERSONALITY:\n{personality_details}"
    if mbti:
        personality_section += (
            f"\nYour MBTI personality type is {mbti}. Let it color how you "
            f"engage, without ever naming or referencing the type itself."
        )
    personality_section += "\n\n"

    return (
        f"You are {partner_name} chatting with someone new.\n"
        f"Respond naturally as yourself, not as an assistant or helper.\n"
        f"Use your natural tone, vocabulary, punctuation, and conversational style.\n\n"
        f"YOUR COMMUNICATION STYLE:\n{style_snippet}\n\n"
        f"{personality_section}"
        f"{context_section}"
        f"[YOUR_CONVERSATION_HISTORY] - This is from your past conversations (you are the user):\n\n"
        f"{samples_text}\n\n"
        f"Use these conversations to know what you've shared about yourself before.\n"
        f"YOU MUST SAY who you are (your FULL NAME) when greeting ONLY THE FIRST TIME.\n\n"
        f"IMPORTANT RULES:\n{rules_prompt}\n\n"
        f"{classification_prompt}\n"
    )
