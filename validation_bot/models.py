import datetime
from typing import List, Optional
from pydantic import BaseModel, Field

class MessageTurn(BaseModel):
    question: str
    raw_question: Optional[str] = None
    user_answer: str
    is_masked: bool = False

class ValidationPair(MessageTurn):
    """Represents a single Q&A match for validation."""
    proxy_answer: str
    similarity: float


class ValidationDocument(BaseModel):
    """Represents the entire database document for a user's validation."""
    user_id: str
    conversation: List[ValidationPair] = Field(default_factory=list)
    average_similarity: float = 0.0
    masked_conversation: List[ValidationPair] = Field(default_factory=list)
    average_masked_similarity: float = 0.0
    
    def to_mongo_update_dict(self) -> dict:
        """
        Converts the document to a MongoDB update payload 
        hiding the raw $set syntax from the business logic.
        """
        now = datetime.datetime.utcnow()
        return {
            "$set": {
                "conversation": [pair.model_dump() for pair in self.conversation],
                "average_similarity": self.average_similarity,
                "updated_at": now
            },
            "$setOnInsert": {
                "created_at": now
            }
        }

    def to_masked_mongo_update_dict(self) -> dict:
        """
        Converts the document to a MongoDB update payload 
        for the masked validation results specifically.
        """
        now = datetime.datetime.utcnow()
        return {
            "$set": {
                "masked_conversation": [pair.model_dump() for pair in self.masked_conversation],
                "average_masked_similarity": self.average_masked_similarity,
                "updated_at": now
            },
            "$setOnInsert": {
                "created_at": now
            }
        }
