"""Slack-free proxy core (Phase 0).

Public surface:
  - ProxyEngine: the one call a web route makes
  - generate_reply: the pure turn function (for tests / proxy-to-proxy)
  - ProxyDefinition / ProxyDefinitionCache: read-only per-user proxy data
  - ProxySession / InMemorySessionStore: per-conversation isolated state
  - ProxyResponse / ReplyResult / UnansweredQuestion: data contracts
  - OpenAIProxyLLM / ProxyLLM: real + injectable LLM
"""
from .proxy_engine import ProxyEngine, generate_reply
from .proxy_definition import (
    ProxyDefinition,
    ProxyDefinitionCache,
    record_to_definition,
)
from .sessions import ProxySession, InMemorySessionStore, SessionStore
from .schemas import ProxyResponse, ReplyResult, UnansweredQuestion
from .llm import OpenAIProxyLLM, ProxyLLM, get_proxy_model

# Interview (learning bot) side
from .interview import (
    InterviewEngine,
    run_interview_turn,
    InterviewState,
    InterviewTurnResult,
    InMemoryInterviewStore,
)
from .question_bank import QuestionBank, default_question_bank, question_bank_v2
from .interview import submit_structured_answer
from .spc_scoring import score_tipi, score_pvq
from .training_compiler import (
    CompiledTraining,
    compile_training,
    default_persist,
    decomposed_pairs_to_qa_items,
)
from .interview_llm import OpenAIInterviewLLM, InterviewLLM, get_learning_model
from .interview_prompt import get_interview_prompt
from .explore import (
    UserFeatures,
    build_user_features,
    score_pair,
    rank_candidates,
    MongoExploreStore,
)

__all__ = [
    # proxy side
    "ProxyEngine",
    "generate_reply",
    "ProxyDefinition",
    "ProxyDefinitionCache",
    "record_to_definition",
    "ProxySession",
    "InMemorySessionStore",
    "SessionStore",
    "ProxyResponse",
    "ReplyResult",
    "UnansweredQuestion",
    "OpenAIProxyLLM",
    "ProxyLLM",
    "get_proxy_model",
    # interview side
    "InterviewEngine",
    "run_interview_turn",
    "InterviewState",
    "InterviewTurnResult",
    "InMemoryInterviewStore",
    "QuestionBank",
    "default_question_bank",
    "question_bank_v2",
    "submit_structured_answer",
    "score_tipi",
    "score_pvq",
    "CompiledTraining",
    "compile_training",
    "default_persist",
    "decomposed_pairs_to_qa_items",
    "OpenAIInterviewLLM",
    "InterviewLLM",
    "get_learning_model",
    "get_interview_prompt",
    "UserFeatures",
    "build_user_features",
    "score_pair",
    "rank_candidates",
    "MongoExploreStore",
]
