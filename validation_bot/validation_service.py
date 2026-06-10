import logging
import json
import numpy as np
import random
from typing import List, Tuple

from openai import OpenAI

from commons.clients import make_slack_client, make_openai_client, get_openai_model
from proxy_bot.proxy_bot_service import get_stateless_proxy_bot_answer

from commons.db import (
    get_mongo_client,
    get_db,
)

from validation_bot.models import ValidationPair, ValidationDocument
from validation_bot.validation_repository import ValidationRepository
from validation_bot.organize_messages_service import OrganizeMessagesService

# ---- Logging --------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("validation_service")

# ---- Clients (Module level initialization for legacy support or easy access) ----
web, BOT_USER_ID = make_slack_client("PROXY_SLACK_BOT_TOKEN")
oai = make_openai_client()
OPENAI_MODEL = get_openai_model("PROXY_MODEL", "gpt-5-mini")


class ValidationService:
    """Core service for computing validations and similarities."""
    
    def __init__(self, openai_client: OpenAI, repository: ValidationRepository, organizer: OrganizeMessagesService = None):
        self.openai_client = openai_client
        self.repository = repository
        self.organizer = organizer or OrganizeMessagesService(openai_client, OPENAI_MODEL)

    def extract_qa_pairs(self, messages: list) -> Tuple[List[str], List[str]]:
        """
        Extracts question and user_answer pairs from a list of messages.
        Groups consecutive user messages together as a single answer to the preceding assistant question.
        Returns a tuple of two lists of equal length: (questions, user_answers)
        """
        questions = []
        user_answers = []
        
        current_question = None
        current_answer_parts = []

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            
            if role == "assistant":
                # If we have a pending question that received an answer, save it.
                if current_question and current_answer_parts:
                    questions.append(current_question)
                    user_answers.append(" \n ".join(current_answer_parts))
                
                current_question = content
                current_answer_parts = []
                
            elif role == "user":
                # Only record answers if we've seen at least one assistant question
                if current_question is not None:
                    current_answer_parts.append(content)
        
        # Don't forget to save the last pair if the conversation ended with an answer
        if current_question and current_answer_parts:
            questions.append(current_question)
            user_answers.append(" \n ".join(current_answer_parts))
            
        return questions, user_answers

    def calculate_similarities(self, user_answers: List[str], proxy_answers: List[str]) -> List[float]:
        """
        Calculate the cosine similarity between corresponding user and proxy answers
        using the OpenAI text-embedding-3-small model.
        """
        if not user_answers or not proxy_answers:
            return []
            
        sentences = user_answers + proxy_answers
        
        response = self.openai_client.embeddings.create(
            input=sentences,
            model="text-embedding-3-small"
        )
        
        embeddings = np.array([data.embedding for data in response.data])
        
        n = len(user_answers)
        user_emb = embeddings[:n]
        proxy_emb = embeddings[n:]
        
        u_norm = np.linalg.norm(user_emb, axis=1, keepdims=True)
        p_norm = np.linalg.norm(proxy_emb, axis=1, keepdims=True)
        
        u_normalized = user_emb / u_norm
        p_normalized = proxy_emb / p_norm
        
        similarities = np.sum(u_normalized * p_normalized, axis=1)
        return [float(sim) for sim in similarities]

    def run_validation(self, user_id: str) -> ValidationDocument:
        """
        Extracts Q&A pairs for the given user, queries the proxy bot in a stateless
        manner, computes similarities, and returns a ValidationDocument.
        """
        messages = self.repository.get_messages_by_user(user_id)
        questions, user_answers = self.extract_qa_pairs(messages)
        
        proxy_bot_answers = []
        for q in questions:
            raw_answer = get_stateless_proxy_bot_answer(
                text=q, 
                requester_user_id=user_id
            )
            
            try:
                parsed = json.loads(raw_answer)
                answer = parsed.get("response", raw_answer) if isinstance(parsed, dict) else raw_answer
            except Exception:
                answer = raw_answer
                
            proxy_bot_answers.append(answer)
             
        similarities = self.calculate_similarities(user_answers, proxy_bot_answers)
             
        pairs = []
        for i, (q, u_ans, p_ans) in enumerate(zip(questions, user_answers, proxy_bot_answers)):
            sim = similarities[i] if i < len(similarities) else 0.0
            pairs.append(ValidationPair(
                question=q,
                user_answer=u_ans,
                proxy_answer=p_ans,
                similarity=sim
            ))
            
        average_similarity = sum(p.similarity for p in pairs) / len(pairs) if pairs else 0.0
             
        return ValidationDocument(
            user_id=user_id,
            conversation=pairs,
            average_similarity=average_similarity
        )

    def run_and_save_validation(self, user_id: str):
        """
        Orchestrates the entire validation flow and saves the result to the DB.
        """
        document = self.run_validation(user_id)
        self.repository.upsert_validation(document)

    def run_validation_with_masking(self, user_id: str, masking_questions: int) -> ValidationDocument:
        """
        Extracts Q&A pairs using LLM, randomly masks 'masking_questions' number of questions,
        queries the proxy bot WITHOUT the masked answers in context, computes similarities, 
        and returns a ValidationDocument.
        """
        messages = self.repository.get_messages_by_user(user_id)
        log.info(f"Retrieved {len(messages)} raw messages for user {user_id}")
        
        # 1. Organize messages with LLM
        organized_turns = self.organizer.organize(messages)
        log.info(f"LLM organized messages into {len(organized_turns)} structured Q&A pairs.")
        
        # 2. Randomly select turns to mask
        num_to_mask = min(masking_questions, len(organized_turns))
        turns_to_mask = random.sample(organized_turns, num_to_mask)
        
        masked_texts_to_exclude = set()
        for turn in turns_to_mask:
            turn.is_masked = True
            masked_texts_to_exclude.add(turn.raw_question.strip())
            masked_texts_to_exclude.add(turn.user_answer.strip())
            
        # Build custom_samples preserving all non-masked chit-chat
        custom_samples = []
        for m in messages:
            content = m.get("content", "").strip()
            role = m.get("role", "")
            
            # If this message was masked, completely drop it
            if content in masked_texts_to_exclude:
                continue
                
            if role in ("assistant", "user") and content:
                prefix = "Question: " if role == "assistant" else "user: "
                custom_samples.append(prefix + content)
                
        log.info(f"Built custom_samples context of size {len(custom_samples)}. Masked texts omitted: {len(masked_texts_to_exclude)}")
            
        questions = [turn.question for turn in organized_turns]
        user_answers = [turn.user_answer for turn in organized_turns]
        
        # 3. Query Proxy Bot
        proxy_bot_answers = []
        for q in questions:
            raw_answer = get_stateless_proxy_bot_answer(
                text=q, 
                requester_user_id=user_id,
                custom_samples=custom_samples
            )
            
            try:
                parsed = json.loads(raw_answer)
                answer = parsed.get("response", raw_answer) if isinstance(parsed, dict) else raw_answer
            except Exception:
                answer = raw_answer
                
            proxy_bot_answers.append(answer)
             
        # 4. Calculate similarities
        similarities = self.calculate_similarities(user_answers, proxy_bot_answers)
             
        pairs = []
        for i, (turn, p_ans) in enumerate(zip(organized_turns, proxy_bot_answers)):
            sim = similarities[i] if i < len(similarities) else 0.0
            pairs.append(ValidationPair(
                question=turn.question,
                raw_question=turn.raw_question,
                user_answer=turn.user_answer,
                proxy_answer=p_ans,
                similarity=sim,
                is_masked=getattr(turn, 'is_masked', False)
            ))
            
        average_similarity = sum(p.similarity for p in pairs) / len(pairs) if pairs else 0.0
        log.info(f"Masked validation complete for user {user_id}. Average similarity: {average_similarity:.4f}")
             
        # Create the document. We store the pairs in masked_conversation
        doc = ValidationDocument(
            user_id=user_id,
        )
        doc.masked_conversation = pairs
        doc.average_masked_similarity = average_similarity
        
        return doc

    def run_and_save_validation_with_masking(self, user_id: str, masking_questions: int):
        """
        Orchestrates the masked validation flow and saves the result to the DB.
        """
        log.info(f"Initiating run_validation_with_masking for user {user_id}")
        document = self.run_validation_with_masking(user_id, masking_questions)
        self.repository.upsert_masked_validation(document)
        log.info(f"Successfully saved masked validation results for user {user_id}")

# Optional: Provide a default instance for easier usage
default_validation_repository = ValidationRepository()
default_validation_service = ValidationService(oai, default_validation_repository)
