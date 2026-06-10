import json
import os
import numpy as np
from pathlib import Path
from openai import OpenAI
from typing import List, Dict, Optional, Tuple

# Initialize OpenAI client (lazy initialization)
_oai_client = None


def get_openai_client():
    """Get or create OpenAI client"""
    global _oai_client
    if _oai_client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable not set")
        _oai_client = OpenAI(api_key=api_key)
    return _oai_client


# Cache for question embeddings
_question_embeddings_cache: Dict[str, np.ndarray] = {}
_questions_data: Optional[List[Dict]] = None


def load_questions() -> List[Dict]:
    """Load questions from JSON file"""
    global _questions_data
    if _questions_data is None:
        questions_path = Path(__file__).parent / "questions.json"
        with open(questions_path, "r") as f:
            _questions_data = json.load(f)
    return _questions_data


def get_embedding(text: str) -> np.ndarray:
    """Generate embedding for text using OpenAI API"""
    oai = get_openai_client()
    response = oai.embeddings.create(model="text-embedding-3-small", input=text)
    return np.array(response.data[0].embedding)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two vectors"""
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))


def get_question_embedding(question_id: str, question_text: str) -> np.ndarray:
    """Get embedding for a question, using cache if available"""
    cache_key = f"{question_id}:{question_text}"
    if cache_key not in _question_embeddings_cache:
        _question_embeddings_cache[cache_key] = get_embedding(question_text)
    return _question_embeddings_cache[cache_key]


def get_next_main_question(asked_main_ids: set) -> Optional[Dict]:
    """
    Get the next main question in sequential order (Q1, Q2, Q3... Q35)
    Returns None if all questions have been asked
    """
    questions = load_questions()

    # Find the first question that hasn't been asked
    for q in questions:
        if q["id"] not in asked_main_ids:
            return q

    return None

def get_follow_up_questions(main_id, asked_followup_ids: set) -> List[Dict]:
    questions = load_questions()
    for q in questions:
        if q["id"] == main_id:
            return [f for f in q.get("followups", []) if f["id"] not in asked_followup_ids]
    return []

def select_relevant_followup(
    main_question: Dict, user_response: str, asked_followup_ids: set
) -> Optional[Dict]:
    """
    Select the most relevant followup question based on semantic similarity
    to the user's response using embeddings.

    Args:
        main_question: The main question dict with followups
        user_response: The user's response text
        asked_followup_ids: Set of followup IDs that have already been asked

    Returns:
        The most relevant followup dict, or None if no suitable followup
    """
    if not main_question.get("followups"):
        return None

    # Filter out already asked followups
    available_followups = [
        f for f in main_question["followups"] if f["id"] not in asked_followup_ids
    ]

    if not available_followups:
        return None

    # Generate embedding for user response
    try:
        user_embedding = get_embedding(user_response)
    except Exception as e:
        # If embedding fails, return first available followup
        return available_followups[0]

    # Compute similarity for each followup
    followup_scores = []
    for followup in available_followups:
        try:
            followup_embedding = get_question_embedding(
                followup["id"], followup["question"]
            )
            similarity = cosine_similarity(user_embedding, followup_embedding)
            followup_scores.append((similarity, followup))
        except Exception as e:
            # If embedding fails for a followup, skip it
            continue

    if not followup_scores:
        return available_followups[0]

    # Return the followup with highest similarity
    followup_scores.sort(key=lambda x: x[0], reverse=True)
    return followup_scores[0][1]


def format_question_bank_for_prompt(
    current_main: Optional[Dict],
    available_followups: Optional[List[Dict]] = None,
    asked_ids: Optional[set] = None,
) -> str:
    """
    Format questions for injection into the system prompt.
    Returns a formatted string that matches the original QuestionBank format.

    Args:
        current_main: The main question dict
        available_followups: Specific followups to show (if None, shows unasked ones)
        asked_ids: Set of question IDs that have already been asked (to filter out)
    """
    if current_main is None:
        return ""

    lines = [f"  {current_main['id']}: Main: {current_main['main_question']}"]

    if available_followups:
        # Show only the specified followups (already filtered)
        lines.append("    FollowUps:")
        for followup in available_followups:
            lines.append(f"        - {followup['id']}: {followup['question']}")
    elif current_main.get("followups"):
        # Show only unasked followups
        asked_ids = asked_ids or set()
        unasked_followups = [
            f for f in current_main["followups"] if f["id"] not in asked_ids
        ]
        if unasked_followups:
            lines.append("    FollowUps:")
            for followup in unasked_followups:
                lines.append(f"        - {followup['id']}: {followup['question']}")

    return "\n".join(lines)

