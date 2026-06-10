"""
SPC scoring: TIPI (Big Five) and PVQ-21 (Schwartz values). Pure functions.

TIPI mapping (Gosling et al., 2003), per the question bank doc:
    Extraversion        items 1, 6(R)
    Agreeableness       items 7, 2(R)
    Conscientiousness   items 3, 8(R)
    Neuroticism         items 4, 9(R)
    Openness            items 5, 10(R)
Reverse scoring on the 7-point scale: r(x) = 8 - x. Trait = mean of its two items.

Output keys deliberately use the labels the existing SPC pipeline already
expects ("Negative Emotionality" for TIPI's Neuroticism, "Open-Mindedness" for
Openness) so commons/spc_pipeline.SPC_PROMPT_TEMPLATE and
scores_to_natural_language work unchanged.

PVQ-21 mapping per the doc:
    Self-Direction 1,11 | Stimulation 6,15 | Hedonism 10,21 | Achievement 4,13
    Power 2,17 | Security 5,14 | Conformity 7,16 | Tradition 9,20
    Benevolence 12,18 | Universalism 3,8,19
"""
from __future__ import annotations

from typing import Dict

_NEUTRAL = 4.0


def _r(x: float) -> float:
    return 8.0 - x


def _get(answers: Dict[str, int], key: str) -> float:
    try:
        return float(answers[key])
    except (KeyError, TypeError, ValueError):
        return _NEUTRAL


def score_tipi(answers: Dict[str, int]) -> Dict[str, float]:
    """answers: {"tipi_1": 1..7, ..., "tipi_10": 1..7} -> Big Five scores (1-7)."""
    g = lambda n: _get(answers, f"tipi_{n}")
    return {
        "Extraversion": (g(1) + _r(g(6))) / 2,
        "Agreeableness": (g(7) + _r(g(2))) / 2,
        "Conscientiousness": (g(3) + _r(g(8))) / 2,
        "Negative Emotionality": (g(4) + _r(g(9))) / 2,   # TIPI: Neuroticism
        "Open-Mindedness": (g(5) + _r(g(10))) / 2,        # TIPI: Openness
    }


_PVQ_MAP = {
    "Self-Direction": (1, 11),
    "Stimulation": (6, 15),
    "Hedonism": (10, 21),
    "Achievement": (4, 13),
    "Power": (2, 17),
    "Security": (5, 14),
    "Conformity": (7, 16),
    "Tradition": (9, 20),
    "Benevolence": (12, 18),
    "Universalism": (3, 8, 19),
}


def score_pvq(answers: Dict[str, int]) -> Dict[str, float]:
    """answers: {"pvq_1": 1..7, ..., "pvq_21": 1..7} -> value scores (1-7)."""
    return {
        value: sum(_get(answers, f"pvq_{i}") for i in items) / len(items)
        for value, items in _PVQ_MAP.items()
    }
