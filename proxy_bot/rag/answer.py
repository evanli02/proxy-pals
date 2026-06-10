# rag/answer.py
from typing import List, Dict
from openai import OpenAI

_oai = OpenAI()

RAG_SYSTEM = (
    "You are a helpful assistant. "
    "When prior Q&A with this user is provided, incorporate only strictly relevant facts. "
)

def _build_context(snippets: List[Dict]) -> str:
    blocks = []
    for s in snippets:
        blocks.append(s["qa_text"])
    return "\n---\n".join(blocks[:5])

def answer_with_qa_context(user_question: str, prior_snippets: List[Dict], persona_prompt: str) -> str:
    context = _build_context(prior_snippets)
    messages = [
        {"role": "system", "content": RAG_SYSTEM},
        {"role": "system", "content": persona_prompt},
        {"role": "system", "content": f"[PRIOR_QA]\n{context}"},
        {"role": "user", "content": user_question}
    ]
    resp = _oai.chat.completions.create(model="gpt-5-mini", messages=messages)
    return resp.choices[0].message.content.strip()
