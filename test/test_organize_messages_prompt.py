import pytest
import json
from validation_bot.organize_messages_prompt import (
    get_organize_messages_prompt,
    parse_organized_messages
)
from validation_bot.models import MessageTurn

def test_get_organize_messages_prompt():
    raw_data = [
        {"role": "user", "content": "I like to play tennis."},
        {"role": "assistant", "content": "Do you do any physical exercise or play any sports?"}
    ]
    
    prompt = get_organize_messages_prompt(raw_data)
    
    # Check that the raw data is formatted in the prompt
    assert json.dumps(raw_data, indent=2) in prompt
    assert "[Input Data (Raw Messages)]" in prompt
    assert "Your task is to transform this sequence into a structured, standardized format." in prompt


def test_parse_organized_messages_valid_json():
    json_input = """
    [
        {
            "question": "Do you do any physical exercise or play any sports?",
            "raw_question": "Do you do any physical exercise?",
            "user_answer": "I play tennis on weekends.",
            "is_masked": false
        },
        {
            "question": "What is your favorite season?",
            "raw_question": "What's your favorite season?",
            "user_answer": "Summer because I can go to the beach.",
            "is_masked": true
        }
    ]
    """
    
    result = parse_organized_messages(json_input)
    
    assert len(result) == 2
    assert isinstance(result[0], MessageTurn)
    assert result[0].question == "Do you do any physical exercise or play any sports?"
    assert result[0].raw_question == "Do you do any physical exercise?"
    assert result[0].user_answer == "I play tennis on weekends."
    assert result[0].is_masked is False

    assert result[1].question == "What is your favorite season?"
    assert result[1].raw_question == "What's your favorite season?"
    assert result[1].user_answer == "Summer because I can go to the beach."
    assert result[1].is_masked is True


def test_parse_organized_messages_invalid_json():
    # Missing closing bracket
    json_input = """
    [
        {
            "question": "test",
            "raw_question": "test",
            "user_answer": "test",
            "is_masked": false
        }
    """
    
    result = parse_organized_messages(json_input)
    assert result == []


def test_parse_organized_messages_invalid_schema():
    # Missing required field 'question'
    json_input = """
    [
        {
            "user_answer": "I play tennis on weekends.",
            "raw_question": "test",
            "is_masked": false
        }
    ]
    """
    
    result = parse_organized_messages(json_input)
    assert result == []
