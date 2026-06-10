import pytest
from unittest.mock import MagicMock
from openai import OpenAI

from validation_bot.models import MessageTurn
from validation_bot.organize_messages_service import OrganizeMessagesService

def test_organize_success():
    mock_client = MagicMock(spec=OpenAI)
    
    mock_response = MagicMock()
    mock_message = MagicMock()
    # Provide valid JSON output mimicking the LLM response
    mock_message.content = '''
    [
        {
            "question": "What is your favorite season?",
            "raw_question": "What is your favorite season?",
            "user_answer": "I like summer.",
            "is_masked": false
        }
    ]
    '''
    mock_response.choices = [MagicMock(message=mock_message)]
    
    mock_client.chat.completions.create.return_value = mock_response
    
    service = OrganizeMessagesService(mock_client, "gpt-dummy")
    
    raw_messages = [{"role": "assistant", "content": "What is your favorite season?"}, {"role": "user", "content": "I like summer."}]
    
    result = service.organize(raw_messages)
    
    # Verify OpenAI client was called
    mock_client.chat.completions.create.assert_called_once()
    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-dummy"
    assert "What is your favorite season?" in kwargs["messages"][0]["content"]
    
    # Verify parsing works
    assert len(result) == 1
    assert isinstance(result[0], MessageTurn)
    assert result[0].question == "What is your favorite season?"
    assert result[0].raw_question == "What is your favorite season?"
    assert result[0].user_answer == "I like summer."
    assert result[0].is_masked is False

def test_organize_empty_messages():
    mock_client = MagicMock(spec=OpenAI)
    service = OrganizeMessagesService(mock_client, "gpt-dummy")
    
    result = service.organize([])
    
    assert result == []
    mock_client.chat.completions.create.assert_not_called()

def test_organize_exception_handling():
    mock_client = MagicMock(spec=OpenAI)
    # Force an exception during LLM call
    mock_client.chat.completions.create.side_effect = Exception("API error")
    
    service = OrganizeMessagesService(mock_client, "gpt-dummy")
    
    raw_messages = [{"role": "user", "content": "hello"}]
    
    result = service.organize(raw_messages)
    
    # Should handle gracefully and return empty list
    assert result == []
