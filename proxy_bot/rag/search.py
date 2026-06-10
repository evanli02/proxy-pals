from typing import List, Dict, Any
from openai import OpenAI
from commons.db import get_qa_pairs_collection

_oai = OpenAI()
EMBEDDING_MODEL = "text-embedding-3-small"

def _embed_query(q: str) -> List[float]:
    return _oai.embeddings.create(model=EMBEDDING_MODEL, input=q).data[0].embedding

def search_similar_qa(user_id: str, query: str, k: int = 3) -> List[Dict[str, Any]]:
    col = get_qa_pairs_collection()
    if col is None:
        return []
    qv = _embed_query(query)
    pipe = [
        {
            "$vectorSearch": {
                "index": "qa_emb_index",
                "path": "embedding",
                "queryVector": qv,
                "numCandidates": 200,
                "limit": k,
                "filter": { "user_id": user_id }
            }
        },
        {
            "$project": {
                "_id": 1,
                "qa_id": 1,
                "user_id": 1,
                "channel_id": 1,
                "q_msg_id": 1,
                "a_msg_id": 1,
                "question_text": 1,
                "answer_text": 1,
                "qa_text": 1,
                "score": {"$meta": "vectorSearchScore"}
            }
        }
    ]
    return list(col.aggregate(pipe))
