"""
Explore ranking: content-based compatibility (Tier 1).

score(A,B) = w·[values_sim, personality_sim, interest_overlap,
              lifestyle_sim, semantic_sim]  (z-scored across the candidate
              pool, weights below) + boosts (likes-you, freshness),
then MMR re-ranking so the feed isn't a wall of the viewer's clones.

Signals per user (built once after training, cached in `explore_features`):
  - values: PVQ-21 scores, MEAN-CENTERED before comparing (priorities, not
    scale usage)
  - personality: Big Five, per-trait weighted (Openness matters most for
    friendship; Neuroticism similarity isn't rewarded much)
  - loves/hates: each item embedded; semantic max-match (so "hiking" pairs
    with "trail running"), shared hates deliberately weighted in
  - routine: one embedding of the weekday+weekend write-ups
  - centroid: mean of the user's qa_pair embeddings (semantic catch-all)

Everything external (embedder, Mongo) is injectable; scoring is pure.
"""
from __future__ import annotations

import datetime
import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("core.explore")

WEIGHTS = {"values": 0.30, "personality": 0.15, "interests": 0.25,
           "lifestyle": 0.10, "semantic": 0.20}
TRAIT_WEIGHTS = {"Open-Mindedness": 0.30, "Extraversion": 0.20,
                 "Agreeableness": 0.20, "Conscientiousness": 0.20,
                 "Negative Emotionality": 0.10}
LIKES_YOU_BOOST = 0.40        # in z-units, after combination
FRESHNESS_BOOST = 0.20        # profiles created in the last 7 days
MMR_LAMBDA = 0.25             # diversity penalty strength
CHIP_MATCH_THRESHOLD = 0.72   # loves/hates semantic match for a chip

Embedder = Callable[[List[str]], List[List[float]]]


# --------------------------- vector helpers ---------------------------------

def _cos(a: Optional[List[float]], b: Optional[List[float]]) -> Optional[float]:
    if not a or not b:
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(x * x for x in b)) or 1e-9
    return dot / (na * nb)


def _mean(vecs: List[List[float]]) -> Optional[List[float]]:
    vecs = [v for v in vecs if v]
    if not vecs:
        return None
    n = len(vecs)
    return [sum(v[i] for v in vecs) / n for i in range(len(vecs[0]))]


# --------------------------- features ----------------------------------------

@dataclass
class UserFeatures:
    user_id: str
    values: Dict[str, float] = field(default_factory=dict)
    personality: Dict[str, float] = field(default_factory=dict)
    loves: List[str] = field(default_factory=list)
    hates: List[str] = field(default_factory=list)
    love_embs: List[List[float]] = field(default_factory=list)
    hate_embs: List[List[float]] = field(default_factory=list)
    routine_emb: Optional[List[float]] = None
    centroid: Optional[List[float]] = None
    created_at: Optional[datetime.datetime] = None


def default_embedder(texts: List[str]) -> List[List[float]]:
    """Same model as the RAG store (text-embedding-3-small, 1536-d)."""
    from openai import OpenAI

    resp = OpenAI().embeddings.create(
        model=os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
        input=texts,
    )
    return [d.embedding for d in resp.data]


def _split_list(text: str) -> List[str]:
    return [t.strip() for t in (text or "").split(",") if t.strip()][:12]


def build_user_features(
    user_id: str,
    record: Dict[str, Any],
    qa_embeddings: List[List[float]],
    embedder: Embedder,
    created_at: Optional[datetime.datetime] = None,
) -> UserFeatures:
    """Build the cached feature bundle from a compiled training record."""
    spc = record.get("spc_raw") or {}
    ctx = spc.get("context") or {}
    loves = _split_list(ctx.get("loves", ""))
    hates = _split_list(ctx.get("hates", ""))
    routine = " ".join(t for t in [ctx.get("weekday"), ctx.get("weekend")] if t)

    to_embed, slots = [], []
    for t in loves:
        to_embed.append(t); slots.append("love")
    for t in hates:
        to_embed.append(t); slots.append("hate")
    if routine:
        to_embed.append(routine[:4000]); slots.append("routine")

    love_embs: List[List[float]] = []
    hate_embs: List[List[float]] = []
    routine_emb: Optional[List[float]] = None
    if to_embed:
        try:
            vecs = embedder(to_embed)
            for slot, v in zip(slots, vecs):
                if slot == "love":
                    love_embs.append(v)
                elif slot == "hate":
                    hate_embs.append(v)
                else:
                    routine_emb = v
        except Exception as e:
            log.error(f"[EXPLORE] embedding failed for {user_id}: {e}")

    return UserFeatures(
        user_id=user_id,
        values=spc.get("value_scores") or {},
        personality=spc.get("personality_scores") or {},
        loves=loves, hates=hates,
        love_embs=love_embs, hate_embs=hate_embs,
        routine_emb=routine_emb,
        centroid=_mean(qa_embeddings),
        created_at=created_at,
    )


# --------------------------- component scores --------------------------------

def values_similarity(a: Dict[str, float], b: Dict[str, float]) -> Optional[float]:
    keys = sorted(set(a) & set(b))
    if len(keys) < 3:
        return None
    av = [a[k] for k in keys]; bv = [b[k] for k in keys]
    am = sum(av) / len(av); bm = sum(bv) / len(bv)
    return _cos([x - am for x in av], [x - bm for x in bv])  # mean-centered


def personality_similarity(a: Dict[str, float], b: Dict[str, float]) -> Optional[float]:
    total_w, acc = 0.0, 0.0
    for trait, w in TRAIT_WEIGHTS.items():
        if trait in a and trait in b:
            acc += w * (1.0 - abs(a[trait] - b[trait]) / 6.0)  # 1-7 scale
            total_w += w
    return (acc / total_w) if total_w else None


def _semantic_match(a_embs, b_embs) -> Tuple[Optional[float], List[Tuple[int, float]]]:
    """Symmetric average of best-match sims; also A-side best matches for chips."""
    if not a_embs or not b_embs:
        return None, []
    a_best = []
    for i, av in enumerate(a_embs):
        best = max((_cos(av, bv) or -1.0) for bv in b_embs)
        a_best.append((i, best))
    b_best = [max((_cos(bv, av) or -1.0) for av in a_embs) for bv in b_embs]
    sym = (sum(s for _, s in a_best) / len(a_best) + sum(b_best) / len(b_best)) / 2
    return sym, a_best


def score_pair(A: UserFeatures, B: UserFeatures) -> Tuple[Dict[str, float], List[str]]:
    """Raw component scores (pre z-scoring) + why-chips, viewer A's perspective.
    Chips use A's OWN terms, so they reveal similarity -- never B's exact item."""
    comps: Dict[str, float] = {}
    chips: List[str] = []

    v = values_similarity(A.values, B.values)
    if v is not None:
        comps["values"] = v
    p = personality_similarity(A.personality, B.personality)
    if p is not None:
        comps["personality"] = p

    love_sym, love_best = _semantic_match(A.love_embs, B.love_embs)
    hate_sym, hate_best = _semantic_match(A.hate_embs, B.hate_embs)
    if love_sym is not None or hate_sym is not None:
        comps["interests"] = 0.6 * (love_sym or 0.0) + 0.4 * (hate_sym or 0.0)

    life = _cos(A.routine_emb, B.routine_emb)
    if life is not None:
        comps["lifestyle"] = life
    sem = _cos(A.centroid, B.centroid)
    if sem is not None:
        comps["semantic"] = sem

    for i, s in sorted(love_best, key=lambda t: -t[1])[:2]:
        if s >= CHIP_MATCH_THRESHOLD and i < len(A.loves):
            chips.append(f"You both love {A.loves[i]}")
    for i, s in sorted(hate_best, key=lambda t: -t[1])[:1]:
        if s >= CHIP_MATCH_THRESHOLD and i < len(A.hates):
            chips.append(f"You both can't stand {A.hates[i]}")
    if v is not None and v > 0.5:
        chips.append("Very similar values")
    if life is not None and life > 0.6 and len(chips) < 3:
        chips.append("Similar weekly rhythm")
    return comps, chips[:3]


# --------------------------- ranking ------------------------------------------

def rank_candidates(
    viewer: UserFeatures,
    candidates: List[UserFeatures],
    likes_you: Optional[set] = None,
    now: Optional[datetime.datetime] = None,
) -> List[Dict[str, Any]]:
    """Score, z-normalize per component across the pool, combine, boost,
    then MMR re-rank for diversity. Returns dicts sorted best-first."""
    likes_you = likes_you or set()
    now = now or datetime.datetime.utcnow()

    scored = []
    for c in candidates:
        comps, chips = score_pair(viewer, c)
        scored.append({"features": c, "components": comps, "chips": chips})

    # z-score each component across the pool (guard tiny/uniform pools)
    for name in WEIGHTS:
        vals = [s["components"][name] for s in scored if name in s["components"]]
        if len(vals) >= 2:
            mu = sum(vals) / len(vals)
            sd = math.sqrt(sum((x - mu) ** 2 for x in vals) / len(vals)) or 1e-9
        else:
            mu, sd = (vals[0] if vals else 0.0), 1.0
        for s in scored:
            if name in s["components"]:
                s["components"][name] = (s["components"][name] - mu) / sd

    for s in scored:
        comps = s["components"]
        present = {k: WEIGHTS[k] for k in comps}
        total_w = sum(present.values()) or 1.0
        base = sum(comps[k] * w for k, w in present.items()) / total_w
        c = s["features"]
        if c.user_id in likes_you:
            base += LIKES_YOU_BOOST
            s["chips"] = (["Liked your standin"] + s["chips"])[:3]
        if c.created_at and (now - c.created_at).days <= 7:
            base += FRESHNESS_BOOST
        s["score"] = base

    # MMR: greedy pick, penalizing similarity (qa-centroid) to already-picked
    remaining = sorted(scored, key=lambda s: -s["score"])
    picked: List[Dict[str, Any]] = []
    while remaining:
        def adjusted(s):
            if not picked:
                return s["score"]
            max_sim = max((_cos(s["features"].centroid, p["features"].centroid) or 0.0)
                          for p in picked)
            return s["score"] - MMR_LAMBDA * max(0.0, max_sim)
        best = max(remaining, key=adjusted)
        remaining.remove(best)
        picked.append(best)

    return [{"user_id": s["features"].user_id, "score": round(s["score"], 4),
             "chips": s["chips"]} for s in picked]


# --------------------------- feature store -------------------------------------

class MongoExploreStore:
    """Cached features in `explore_features`; builds lazily from the compiled
    record + qa_pairs embeddings when missing or stale."""

    def __init__(self, embedder: Embedder = default_embedder):
        self._embedder = embedder

    def _db(self):
        from commons.db import get_db
        return get_db()

    def _doc_to_features(self, doc) -> UserFeatures:
        return UserFeatures(
            user_id=doc["user_id"],
            values=doc.get("values") or {},
            personality=doc.get("personality") or {},
            loves=doc.get("loves") or [], hates=doc.get("hates") or [],
            love_embs=doc.get("love_embs") or [], hate_embs=doc.get("hate_embs") or [],
            routine_emb=doc.get("routine_emb"),
            centroid=doc.get("centroid"),
            created_at=doc.get("created_at"),
        )

    def rebuild(self, user_id: str) -> Optional[UserFeatures]:
        db = self._db()
        if db is None:
            return None
        record = db.conversations.find_one({"user_id": user_id})
        if not record:
            return None
        qa_embs = [d["embedding"] for d in
                   db.qa_pairs.find({"user_id": user_id}, {"embedding": 1}).limit(300)
                   if d.get("embedding")]
        user_doc = db.users.find_one({"user_id": user_id}, {"created_at": 1}) or {}
        feats = build_user_features(user_id, record, qa_embs, self._embedder,
                                    created_at=user_doc.get("created_at"))
        db.explore_features.update_one(
            {"user_id": user_id},
            {"$set": {
                "user_id": user_id, "values": feats.values,
                "personality": feats.personality,
                "loves": feats.loves, "hates": feats.hates,
                "love_embs": feats.love_embs, "hate_embs": feats.hate_embs,
                "routine_emb": feats.routine_emb, "centroid": feats.centroid,
                "created_at": feats.created_at,
                "updated_at": datetime.datetime.utcnow(),
            }},
            upsert=True,
        )
        return feats

    def get(self, user_id: str) -> Optional[UserFeatures]:
        db = self._db()
        if db is None:
            return None
        doc = db.explore_features.find_one({"user_id": user_id})
        if doc:
            return self._doc_to_features(doc)
        return self.rebuild(user_id)
