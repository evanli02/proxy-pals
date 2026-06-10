import logging
from typing import List, Dict, Any
from openai import OpenAI
from commons.db import get_qa_pairs_collection

log = logging.getLogger("rag.store")
_oai = OpenAI()

EMBEDDING_MODEL = "text-embedding-3-small"  # 1536-d

def _embed_batch(texts: List[str]) -> List[List[float]]:
    resp = _oai.embeddings.create(model=EMBEDDING_MODEL, input=texts)
    return [d.embedding for d in resp.data]

def upsert_qa_items(qa_items: List[Dict[str, Any]]):
    """
    qa_items: extract_qa_pairs_from_conversation() 결과들
              (embedding 필드는 비어 있음)
    """
    if qa_items is None:
        return
    col = get_qa_pairs_collection()
    if col is None:
        log.warning("qa_pairs collection not available (None returned)")
        return

    texts = [it["qa_text"] for it in qa_items]
    vecs = _embed_batch(texts)

    for it, v in zip(qa_items, vecs):
        doc = {
            "_id": it["_id"],                        # "qa_xxxxxxxx"
            "qa_id": it["qa_id"],
            "user_id": it["user_id"],
            "channel_id": it["channel_id"],
            "q_msg_id": it["q_msg_id"],
            "a_msg_id": it["a_msg_id"],
            "question_text": it["question_text"],
            "answer_text": it["answer_text"],
            "qa_text": it["qa_text"],
            "created_at": it["created_at"],
            "embedding": v
        }
        col.update_one({"_id": it["_id"]}, {"$set": doc}, upsert=True)
