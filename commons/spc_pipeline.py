"""
SPC (Social Identity, Personal Identity, Personal Life Context) pipeline.

Handles:
  - Parsing the SPC_averaged CSV format into structured data
  - Converting BFI-2-S and PVQ scores to natural language descriptions
  - Generating personality descriptions via Chain of Density (CoD) prompting
  - Storing the generated personality into the user's conversations document

Based on the SPeCtrum framework (Lee et al., 2025).
"""

import csv
import io
import logging
from typing import Dict, Any, Optional, List, Tuple

from openai import OpenAI

from commons.db import get_conversations_collection

log = logging.getLogger("spc_pipeline")


def normalize_cornell_id(raw: str) -> str:
    """Extract the net ID from a raw input that might be an email.

    e.g. "el875@cornell.edu" -> "el875", "EL875" -> "el875", "  el875 " -> "el875"
    """
    raw = raw.strip().lower()
    if "@" in raw:
        raw = raw.split("@")[0]
    return raw


# ---------------------------------------------------------------------------
# 0. Convert Qualtrics webhook JSON to parsed SPC data
# ---------------------------------------------------------------------------

# likert text -> numeric (1-7 scale)
LIKERT_MAP = {
    "strongly disagree": 1,
    "disagree": 2,
    "somewhat disagree": 3,
    "neither agree nor disagree": 4,
    "somewhat agree": 5,
    "agree": 6,
    "strongly agree": 7,
}


def _likert_to_num(text: str) -> float:
    """Convert a Likert scale text response to a number (1-7)."""
    val = LIKERT_MAP.get(text.strip().lower())
    if val is not None:
        return float(val)
    # try parsing as a number directly in case format changes
    try:
        return float(text.strip())
    except ValueError:
        log.warning(f"Could not parse Likert value: {text}")
        return 4.0  # neutral fallback


def convert_qualtrics_json_to_spc(data: Dict[str, Any]) -> Dict[str, Any]:
    """Convert the Qualtrics webhook JSON payload to structured SPC data.

    The BFI-2-S (10 items) maps to the Big Five as follows:
      Extraversion:          bfi_extraverted(+), bfi_reserved(R)
      Agreeableness:         bfi_sympathetic(+), bfi_critical(R)
      Conscientiousness:     bfi_dependable(+), bfi_disorganized(R)
      Negative Emotionality: bfi_anxious(+), bfi_calm(R)
      Open-Mindedness:       bfi_open(+), bfi_conventional(R)

    (R) = reverse scored: score = 8 - raw_score

    The PVQ (21 items) maps to 10 value dimensions (Schwartz, 2009):
      Self-Direction:  pvq_creativity, pvq_decisions
      Power:           pvq_rich, pvq_respect
      Universalism:    pvq_equality, pvq_understand, pvq_nature
      Achievement:     pvq_abilities, pvq_successful
      Security:        pvq_security, pvq_government
      Stimulation:     pvq_surprises, pvq_adventures
      Conformity:      pvq_rules, pvq_behave
      Benevolence:     pvq_help, pvq_loyal
      Tradition:       pvq_humble, pvq_tradition
      Hedonism:        pvq_goodtime, pvq_fun
    """

    def _get(key: str) -> float:
        return _likert_to_num(data.get(key, "4"))

    def _reverse(score: float) -> float:
        return 8.0 - score

    def _avg(*scores: float) -> float:
        return sum(scores) / len(scores)

    # --- BFI-2-S Big Five (averaged, with reverse scoring) ---
    personality = {
        "Extraversion": _avg(_get("bfi_extraverted"), _reverse(_get("bfi_reserved"))),
        "Agreeableness": _avg(_get("bfi_sympathetic"), _reverse(_get("bfi_critical"))),
        "Conscientiousness": _avg(_get("bfi_dependable"), _reverse(_get("bfi_disorganized"))),
        "Negative Emotionality": _avg(_get("bfi_anxious"), _reverse(_get("bfi_calm"))),
        "Open-Mindedness": _avg(_get("bfi_open"), _reverse(_get("bfi_conventional"))),
    }

    # --- PVQ value dimensions (averaged) ---
    values = {
        "Self-Direction": _avg(_get("pvq_creativity"), _get("pvq_decisions")),
        "Power": _avg(_get("pvq_rich"), _get("pvq_respect")),
        "Universalism": _avg(_get("pvq_equality"), _get("pvq_understand"), _get("pvq_nature")),
        "Achievement": _avg(_get("pvq_abilities"), _get("pvq_successful")),
        "Security": _avg(_get("pvq_security"), _get("pvq_government")),
        "Stimulation": _avg(_get("pvq_surprises"), _get("pvq_adventures")),
        "Conformity": _avg(_get("pvq_rules"), _get("pvq_behave")),
        "Benevolence": _avg(_get("pvq_help"), _get("pvq_loyal")),
        "Tradition": _avg(_get("pvq_humble"), _get("pvq_tradition")),
        "Hedonism": _avg(_get("pvq_goodtime"), _get("pvq_fun")),
    }

    # --- Context (passthrough) ---
    context = {}
    if data.get("context_loves"):
        context["loves"] = data["context_loves"]
    if data.get("context_hates"):
        context["hates"] = data["context_hates"]
    if data.get("context_weekday"):
        context["weekday"] = data["context_weekday"]
    if data.get("context_weekend"):
        context["weekend"] = data["context_weekend"]

    return {"personality": personality, "values": values, "context": context}


def convert_qualtrics_to_spc(raw_csv: str) -> str:
    """Convert raw Qualtrics CSV export to SPC_averaged CSV format.

    Legacy passthrough for CSV-based ingestion. For the webhook JSON format,
    use convert_qualtrics_json_to_spc() instead.
    """
    return raw_csv


# ---------------------------------------------------------------------------
# 1. Parse the SPC_averaged CSV format
# ---------------------------------------------------------------------------

def parse_spc_csv(csv_text: str) -> Dict[str, Any]:
    """Parse SPC_averaged CSV into structured data.

    Expected format (no header row):
        personality,Extraversion,4.67
        personality,Agreeableness,4.83
        ...
        social value,Self-Direction,1.5
        ...
        context,List at least 5 things you love,"Basketball, ..."
        ...

    Returns:
        {
            "personality": {"Extraversion": 4.67, "Agreeableness": 4.83, ...},
            "values": {"Self-Direction": 1.5, "Power": 2.5, ...},
            "context": {"loves": "...", "hates": "...", "weekday": "...", "weekend": "..."},
        }
    """
    personality = {}
    values = {}
    context = {}

    reader = csv.reader(io.StringIO(csv_text.strip()))
    for row in reader:
        if len(row) < 3:
            continue
        category = row[0].strip().lower()
        label = row[1].strip()
        value = row[2].strip()

        if category == "personality":
            try:
                personality[label] = float(value)
            except ValueError:
                log.warning(f"Could not parse personality score: {label}={value}")

        elif category == "social value":
            try:
                values[label] = float(value)
            except ValueError:
                log.warning(f"Could not parse value score: {label}={value}")

        elif category == "context":
            key = label.lower()
            if "love" in key:
                context["loves"] = value
            elif "hate" in key:
                context["hates"] = value
            elif "weekday" in key:
                context["weekday"] = value
            elif "weekend" in key:
                context["weekend"] = value

    return {"personality": personality, "values": values, "context": context}


# ---------------------------------------------------------------------------
# 2. Convert scores to natural language (following SPeCtrum methodology)
# ---------------------------------------------------------------------------

# BFI-2-S is on a 1-7 Likert scale, midpoint 4
# PVQ is also on a 1-7 scale (per the paper's methodology)

def _score_to_level(score: float, midpoint: float = 4.0) -> str:
    """Convert a 1-7 Likert score to a descriptive level."""
    diff = score - midpoint
    if diff <= -2.0:
        return "very low"
    elif diff <= -1.0:
        return "below average"
    elif diff <= -0.3:
        return "slightly below average"
    elif diff <= 0.3:
        return "average"
    elif diff <= 1.0:
        return "slightly above average"
    elif diff <= 2.0:
        return "above average"
    else:
        return "very high"


def scores_to_natural_language(
    personality_scores: Dict[str, float],
    value_scores: Dict[str, float],
) -> Tuple[str, str]:
    """Convert BFI-2-S and PVQ scores into natural language descriptions.

    Returns: (personality_description, values_description)
    """
    # personality
    personality_lines = []
    for trait, score in personality_scores.items():
        level = _score_to_level(score)
        personality_lines.append(f"{trait} is {level} ({score:.1f}/7)")
    personality_desc = "Big Five Personality Traits (BFI-2-S):\n" + "\n".join(personality_lines)

    # values
    value_lines = []
    for value_name, score in value_scores.items():
        level = _score_to_level(score)
        value_lines.append(f"{value_name} is {level} ({score:.1f}/7)")
    values_desc = "Personal Values (PVQ):\n" + "\n".join(value_lines)

    return personality_desc, values_desc


# ---------------------------------------------------------------------------
# 3. Generate personality description
# ---------------------------------------------------------------------------

SPC_PROMPT_TEMPLATE = """\
You are a psychologist and personality assessment expert.
Your task is to translate psychometric scale scores into a natural, human-readable description of personality and values.
The description should:
- Be concise but informative
- Describe behavioral tendencies
- Use natural language
- Avoid mentioning numerical scores
- Reflect relative strengths and weaknesses

Here are the personality scores:
Extraversion: {extraversion}/7
Agreeableness: {agreeableness}/7
Conscientiousness: {conscientiousness}/7
Negative Emotionality: {negative_emotionality}/7
Open-Mindedness: {open_mindedness}/7

Here are the value scores:
Self-Direction: {self_direction}/7
Power: {power}/7
Universalism: {universalism}/7
Benevolence: {benevolence}/7
Tradition: {tradition}/7
Hedonism: {hedonism}/7
Stimulation: {stimulation}/7
Conformity: {conformity}/7
Security: {security}/7
Achievement: {achievement}/7

Write a personality and values profile in two-paragraph form."""


def generate_personality_description(
    parsed_data: Dict[str, Any],
    oai: OpenAI,
    model: str = "gpt-5.4",
) -> str:
    """Generate a personality description from SPC scores.

    Fills in the prompt template with the parsed scores and calls OpenAI
    to produce a two-paragraph personality and values profile.
    """
    p = parsed_data.get("personality", {})
    v = parsed_data.get("values", {})

    prompt = SPC_PROMPT_TEMPLATE.format(
        extraversion=f"{p.get('Extraversion', 4.0):.2f}",
        agreeableness=f"{p.get('Agreeableness', 4.0):.2f}",
        conscientiousness=f"{p.get('Conscientiousness', 4.0):.2f}",
        negative_emotionality=f"{p.get('Negative Emotionality', 4.0):.2f}",
        open_mindedness=f"{p.get('Open-Mindedness', 4.0):.2f}",
        self_direction=f"{v.get('Self-Direction', 4.0):.2f}",
        power=f"{v.get('Power', 4.0):.2f}",
        universalism=f"{v.get('Universalism', 4.0):.2f}",
        benevolence=f"{v.get('Benevolence', 4.0):.2f}",
        tradition=f"{v.get('Tradition', 4.0):.2f}",
        hedonism=f"{v.get('Hedonism', 4.0):.2f}",
        stimulation=f"{v.get('Stimulation', 4.0):.2f}",
        conformity=f"{v.get('Conformity', 4.0):.2f}",
        security=f"{v.get('Security', 4.0):.2f}",
        achievement=f"{v.get('Achievement', 4.0):.2f}",
    )

    try:
        resp = oai.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )
        result = resp.choices[0].message.content or ""
        return result.strip()

    except Exception as e:
        log.error(f"Failed to generate personality description: {e}")
        # fallback: return the raw scores as text
        personality_desc, values_desc = scores_to_natural_language(p, v)
        return f"{personality_desc}\n\n{values_desc}"


# ---------------------------------------------------------------------------
# 4. Full ingestion pipeline
# ---------------------------------------------------------------------------

def ingest_spc_from_webhook(
    webhook_data: Dict[str, Any],
    oai: OpenAI,
    model: str = "gpt-5.4",
) -> Dict[str, Any]:
    """Full pipeline: Qualtrics webhook JSON -> parse -> generate personality -> store in DB.

    Accepts the raw JSON payload from the Qualtrics webhook.
    Looks up the user by cornell_id in the conversations collection,
    then writes the generated personality to their document.

    Returns a status dict with details about what happened.
    """
    cornell_id = normalize_cornell_id(webhook_data.get("cornell_id", ""))
    if not cornell_id:
        return {"status": "error", "message": "cornell_id missing from webhook data"}

    # convert qualtrics JSON to structured SPC data
    parsed = convert_qualtrics_json_to_spc(webhook_data)
    if not parsed["personality"] and not parsed["values"]:
        return {"status": "error", "message": "No personality or value scores found in webhook data"}

    log.info(
        f"[SPC] Parsed webhook for {cornell_id}: "
        f"{len(parsed['personality'])} personality traits, "
        f"{len(parsed['values'])} values, "
        f"{len(parsed['context'])} context fields"
    )

    # generate personality description
    personality_text = generate_personality_description(parsed, oai, model)
    log.info(f"[SPC] Generated personality description for {cornell_id} ({len(personality_text)} chars)")

    # find user in DB by cornell_id
    conv_col = get_conversations_collection()
    if conv_col is None:
        return {"status": "error", "message": "Database not available"}

    # store personality and raw SPC data
    # upsert=True so it works even if the user hasn't chatted with the
    # learning bot yet (survey-first flow). if the document already exists,
    # this overwrites the personality fields (handles resubmission).
    import datetime
    conv_col.update_one(
        {"cornell_id": cornell_id},
        {
            "$set": {
                "cornell_id": cornell_id,
                "personality": personality_text,
                "spc_raw": {
                    "personality_scores": parsed["personality"],
                    "value_scores": parsed["values"],
                    "context": parsed["context"],
                },
                "spc_updated_at": datetime.datetime.utcnow(),
            }
        },
        upsert=True,
    )

    log.info(f"[SPC] Stored personality for cornell_id={cornell_id}")
    return {
        "status": "ok",
        "cornell_id": cornell_id,
        "personality_preview": personality_text[:200] + "...",
    }


def ingest_spc_for_user(
    cornell_id: str,
    csv_text: str,
    oai: OpenAI,
    model: str = "gpt-5.4",
) -> Dict[str, Any]:
    """Legacy CSV-based pipeline. Use ingest_spc_from_webhook for the Qualtrics webhook format."""
    csv_text = convert_qualtrics_to_spc(csv_text)
    parsed = parse_spc_csv(csv_text)
    if not parsed["personality"] and not parsed["values"]:
        return {"status": "error", "message": "No personality or value scores found in CSV"}

    personality_text = generate_personality_description(parsed, oai, model)

    conv_col = get_conversations_collection()
    if conv_col is None:
        return {"status": "error", "message": "Database not available"}

    doc = conv_col.find_one({"cornell_id": cornell_id})
    if doc is None:
        return {
            "status": "error",
            "message": f"No user found with cornell_id={cornell_id}.",
        }

    import datetime
    conv_col.update_one(
        {"cornell_id": cornell_id},
        {
            "$set": {
                "personality": personality_text,
                "spc_raw": {
                    "personality_scores": parsed["personality"],
                    "value_scores": parsed["values"],
                    "context": parsed["context"],
                },
                "spc_updated_at": datetime.datetime.utcnow(),
            }
        },
    )

    return {
        "status": "ok",
        "cornell_id": cornell_id,
        "personality_preview": personality_text[:200] + "...",
    }
