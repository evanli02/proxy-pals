import logging
import json
from textwrap import dedent
from typing import List

from pydantic import TypeAdapter

from validation_bot.models import MessageTurn

ORGANIZE_MESSAGES_PROMPT = dedent(
    """

  [WHO YOU ARE?]
  You are a Precise Data Engineer specializing in NLP. 

  [CONTEXT]
  I am providing a raw conversation log. The Bot often includes reactions and follow-up questions. 
  You must identify the 'Core Question' that actually prompted the user's answer.

  [GOAL]
  Your task is to transform this sequence into a structured, standardized format. 
  Each entry must pair the assistant's core question with the user's specific response to that question.

  [Reference Questions]
  Map the core intent of each bot message to the most relevant question from this list:
  - What did you want to be when you were younger?
  - Do you think that's related to what you are doing now?
  - Where do you see yourself 5 years from now?
  - What do you need to do to get to where you see yourself?
  - Do you do any physical exercise or play any sports?
  - How did you get into that?
  - How about some static activity? What was the last movie you've seen?
  - What did you think of it?
  - Do you listen to music as well?
  - What's your favorite artist or band?
  - Do you have a favorite song or album?
  - What is your favorite season?
  - What's your favorite activity to do during this season?
  - Where was the last place you went on vacation?
  - What was the most memorable thing about that trip?
  - Would you prefer to live in the city or a rural area?
  - What is your favorite food?
  - Are there any foods that you dislike or will not eat?
  - What is your favorite restaurant in Ithaca?
  - What is the signature dish that you cook?

  [Task Instructions]
  1. Analyze Every Turn: Iterate through the provided conversation data.
  2. Extract Core Question: Strip away conversational filler and reactions. 
  3. Map & Standardize: Align the extracted question with the style of the Reference Questions..
  4. Set Boolean Flag: Set `is_masked` to false.
  
  [Input Data (Raw Messages)]
  {RAW_DATA_STRING}

  [Output Format]
  Return ONLY a JSON array of objects. Do not include any explanation.
  Example:
  [
    {{
      "question": "Standardized question text",
      "raw_question": "Original bot question text",
      "user_answer": "Original user answer",
      "is_masked": false
    }}
   ]
  """
).strip()

def get_organize_messages_prompt(
    raw_data: list
) -> str:
    return ORGANIZE_MESSAGES_PROMPT.format(
        RAW_DATA_STRING=json.dumps(raw_data, indent=2)
    )

def parse_organized_messages(llm_output: str) -> List[MessageTurn]:
    try:
        adapter = TypeAdapter(List[MessageTurn])
        return adapter.validate_json(llm_output)
    except Exception as e:
        logging.error(f"Parsing failed: {e}")
        return []