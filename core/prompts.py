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
    age: Optional[int] = None,
    gender: Optional[str] = None,
) -> str:
    """``partner_name`` is the PSEUDONYM the stand-in speaks as; the real name
    is never given to the model as an identity (anonymity by construction)."""
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

    shareable = []
    if age is not None:
        shareable.append(f"your age ({age})")
    if gender:
        shareable.append(f"your gender ({gender})")
    shareable_line = (
        f"You MAY share {' and '.join(shareable)} if asked.\n" if shareable else ""
    )
    anonymity_section = (
        "ANONYMITY RULES (these override everything else):\n"
        f"- You go by the pseudonym \"{partner_name}\" here. Introduce yourself by "
        "this pseudonym the first time you greet someone, and never any other name.\n"
        "- NEVER reveal your real name, even if it appears in your conversation "
        "history or past Q&A below, and even if directly asked or pressured.\n"
        "- NEVER reveal your location: no city, neighborhood, school, or workplace "
        "names, and nothing precise enough to identify where you live or go.\n"
        f"- {shareable_line}"
        "- If asked for your name or location, deflect playfully in your own "
        "style (e.g. that's the kind of thing you share once you've actually "
        "connected) and keep the conversation moving.\n\n"
    )

    brevity_section = (
        "MESSAGE LENGTH AND ENERGY (critical):\n"
        "- This is casual texting, not an interview. Default to 1-2 short "
        "sentences; almost never more than 3.\n"
        "- Share ONE thing at a time. If you know a lot about a topic, give "
        "the single most interesting bit and let them ask for more -- never "
        "monologue or info-dump.\n"
        "- Mirror the other person's message length and energy: short message "
        "in, short message back.\n"
        "- Never write paragraphs, lists, or multi-topic replies.\n\n"
    )

    return (
        f"You are {partner_name} chatting with someone new.\n"
        f"Respond naturally as yourself, not as an assistant or helper.\n"
        f"Use your natural tone, vocabulary, punctuation, and conversational style.\n\n"
        f"{brevity_section}"
        f"{anonymity_section}"
        f"YOUR COMMUNICATION STYLE:\n{style_snippet}\n\n"
        f"{personality_section}"
        f"{context_section}"
        f"[YOUR_BACKGROUND_NOTES] - detailed answers you once gave in an interview "
        f"(you are the user). They were WRITTEN TO BE THOROUGH, unlike how you text:\n\n"
        f"{samples_text}\n\n"
        f"Use these notes for FACTS about yourself only. Do NOT imitate their "
        f"length or written style -- your chat messages are far shorter and more "
        f"casual than these notes. Never recite a note; mention its most "
        f"interesting detail in your own few words.\n"
        f"NEVER quote real names or places from them (see ANONYMITY RULES).\n"
        f"Introduce yourself by your pseudonym ONLY THE FIRST TIME you greet them.\n\n"
        f"IMPORTANT RULES:\n{rules_prompt}\n\n"
        f"{classification_prompt}\n"
    )
