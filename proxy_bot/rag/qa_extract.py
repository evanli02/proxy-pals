import hashlib
from datetime import datetime
from typing import List, Dict, Any


from commons.db import get_mongo_client, get_db, get_conversations_collection

mongo_client = get_mongo_client()
db = get_db() if mongo_client is not None else None

def is_question(msg: dict) -> bool:
    text = (msg.get("content") or "").strip()
    if not text:
        return False
    qs_mark = "?" in text
    return qs_mark

def _msg_id(msg: Dict[str, Any], fallback_index: int) -> str:
    return msg.get("id") or msg.get("_id") or f"m_{fallback_index}"

def _make_qa_id(user_id: str, q_msg_id: str, a_msg_id: str) -> str:
    base = f"{user_id}::{q_msg_id}::{a_msg_id}"
    h = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
    return f"qa_{h}"

def extract_qa_pairs_from_conversation(conv_doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    conv_doc: conversations 문서 1개
    returns: list of qa_items dicts that match the target schema (embedding 제외)
    """
    items: List[Dict[str, Any]] = []
    msgs = conv_doc.get("messages", []) or []
    user_id = conv_doc.get("user_id") or ""
    channel_id = conv_doc.get("channel_id") or ""
    created_at = conv_doc.get("updated_at") or datetime.utcnow().timestamp()
    print("msgs: ", msgs)
    pending_q = None
    pending_q_id = None

    for i, m in enumerate(msgs):
        role = m.get("role")
        content = (m.get("content") or "").strip()
        print("msgs: ", content)
        print("m: ", m)
        print("is_question: ", is_question(m))

        if not content:
            continue

        if role == "assistant":
            if is_question(m):
                pending_q = content
                pending_q_id = _msg_id(m, i)

        elif role == "user" and pending_q:
            a_text = content
            a_id = _msg_id(m, i)

            qa_id = _make_qa_id(user_id, pending_q_id, a_id)
            qa_text = f"Q: {pending_q}\nA: {a_text}"

            items.append({
                "_id": qa_id,                      # 문서 기본키로 사용
                "qa_id": qa_id,                    # (선택) 레거시 호환
                "user_id": user_id,                # 답변자(= 유저)
                "channel_id": channel_id,
                "q_msg_id": pending_q_id,
                "a_msg_id": a_id,
                "question_text": pending_q,
                "answer_text": a_text,
                "qa_text": qa_text,
                "created_at": created_at
                # "embedding": ...  # 여기서는 미포함 (store 단계에서 주입)
            })
            pending_q = None
            pending_q_id = None

    return items