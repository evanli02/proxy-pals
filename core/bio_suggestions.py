"""
Bio suggestions: 3-5 one-sentence bios grounded in the user's training data.

Pulls from the compiled record (loves/hates, routines, personality, and a few
of the user's own messages so suggestions can echo their voice). The generator
is injectable; the default uses OpenAI JSON mode.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

log = logging.getLogger("core.bio_suggestions")

BIO_PROMPT = """\
Write bio suggestions for a social app profile. A bio is ONE short sentence \
(under 120 characters) in the user's own voice -- it can be a funny remark, a \
vivid self-description, or a brief intro. Ground every suggestion in the \
provided training data; never invent facts. Mirror their texting style where \
you can. Vary the register across suggestions: at least one playful, at least \
one straightforward.

Respond ONLY with JSON: {"suggestions": ["...", "...", "..."]} (3 to 5 items).
"""


def build_bio_context(record: Dict[str, Any]) -> str:
    spc = record.get("spc_raw") or {}
    ctx = spc.get("context") or {}
    parts = []
    if ctx.get("loves"):
        parts.append(f"Loves: {ctx['loves']}")
    if ctx.get("hates"):
        parts.append(f"Hates: {ctx['hates']}")
    if record.get("personality"):
        parts.append(f"Personality: {record['personality'][:600]}")
    own_lines = [
        m.get("content", "") for m in record.get("messages", [])
        if m.get("role") == "user" and m.get("content")
        and not (m.get("metadata") or {}).get("skipped")
    ][:12]
    if own_lines:
        parts.append("Their own messages (voice sample):\n- " + "\n- ".join(own_lines))
    return "\n\n".join(parts) or "(no training data)"


def default_bio_generator(record: Dict[str, Any]) -> List[str]:
    from openai import OpenAI

    try:
        resp = OpenAI().chat.completions.create(
            model=os.environ.get("LEARNING_MODEL", "gpt-5-mini"),
            messages=[
                {"role": "system", "content": BIO_PROMPT},
                {"role": "user", "content": build_bio_context(record)},
            ],
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        out = [s.strip() for s in data.get("suggestions", []) if isinstance(s, str) and s.strip()]
        return out[:5]
    except Exception as e:
        log.error(f"[BIO] generation failed: {e}")
        return []
