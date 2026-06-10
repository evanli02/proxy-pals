import logging
from typing import List
from openai import OpenAI

from validation_bot.models import MessageTurn
from validation_bot.organize_messages_prompt import (
    get_organize_messages_prompt,
    parse_organized_messages
)

log = logging.getLogger("organize_messages_service")

class OrganizeMessagesService:
    """Service to handle organizing raw messages into standardized MessageTurn objects using an LLM."""
    
    def __init__(self, openai_client: OpenAI, model: str):
        self.openai_client = openai_client
        self.model = model

    def organize(self, raw_messages: list) -> List[MessageTurn]:
        """
        Takes raw messages from a conversation and uses an LLM to organize them
        into an array of core questions and user answers.
        """
        if not raw_messages:
            return []

        prompt = get_organize_messages_prompt(raw_messages)
        
        try:
            response = self.openai_client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0
            )
            llm_output = response.choices[0].message.content
            return parse_organized_messages(llm_output)
        except Exception as e:
            log.error(f"Failed to organize messages using LLM: {e}")
            return []
