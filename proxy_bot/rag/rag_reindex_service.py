from typing import Optional, Dict, Any
from datetime import datetime
from pymongo.collection import Collection

from commons.db import get_conversations_collection
from store import upsert_qa_items       # 임베딩+업서트
from qa_extract import extract_qa_pairs_from_conversation

def _build_query(since_ts: Optional[float], user_id: Optional[str]) -> Dict[str, Any]:
    q: Dict[str, Any] = {}
    if since_ts is not None:
        q["updated_at"] = {"$gte": since_ts}
    if user_id:
        q["user_id"] = user_id
    return q

def reindex_all_conversations(
        since_ts: Optional[float] = None,   # 예: time.time() - 7*86400
        user_id: Optional[str] = None,      # 특정 유저만
        limit: Optional[int] = None,        # 최대 문서 수
        projection: Optional[Dict[str, int]] = None,
        dry_run: bool = False,              # 임베딩/업서트 없이 숫자만 집계
) -> Dict[str, Any]:
    conv_col: Collection = get_conversations_collection()
    proj = projection or {
        "messages": 1,
        "updated_at": 1,
        "user_id": 1,
        "channel_id": 1,
    }
    query = _build_query(since_ts, user_id)
    cur = conv_col.find(query, projection=proj).batch_size(200)
    if limit:
        cur = cur.limit(int(limit))

    stats = {
        "conversations_scanned": 0,
        "conversations_with_qa": 0,
        "qa_extracted": 0,
        "qa_upserted": 0,
        "since_ts": since_ts,
        "user_id": user_id,
        "dry_run": dry_run,
    }

    for conv in cur:
        stats["conversations_scanned"] += 1
        qa_items = extract_qa_pairs_from_conversation(conv)
        if not qa_items:
            continue
        stats["conversations_with_qa"] += 1
        stats["qa_extracted"] += len(qa_items)
        if not dry_run:
            upsert_qa_items(qa_items)
            stats["qa_upserted"] += len(qa_items)

    stats["finished_at"] = datetime.utcnow().isoformat()
    return stats