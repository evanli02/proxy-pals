# Conversation Style Analysis Script

This script analyzes all conversations stored in MongoDB and performs detailed analysis of each user's message style.

## Features

- **Formality Analysis**: Analyzes whether users use formal/informal language
- **Rhythm Analysis**: Analyzes sentence length and conversation rhythm patterns
- **Punctuation Analysis**: Analyzes punctuation usage patterns and characteristics
- **Emoji and Markers**: Analyzes emoji usage frequency and conversation markers
- **Vocabulary Analysis**: Analyzes frequently used words, hedges, and intensifiers
- **Conversation Moves**: Analyzes conversation opening, clarification, topic shifting, boundary setting, and closing patterns
- **Style Rules**: Generates personalized style guidelines

## Usage

### 1. Environment Variables Setup
```bash
# Check if MONGODB_URI is set in .env file
echo $MONGODB_URI
```

### 2. Run Script
```bash
python analyze_conversations.py
```

### 3. Check Results
Analysis results are stored in MongoDB's `user_communication_style` collection.

## Output Format

Analysis results are stored for each user in the following format:

```json
{
  "user_id": "U1234567890",
  "user_name": "John Doe",
  "message_count": 45,
  "communication_style": {
    "summary": "User communicates in a casual style with short_bursts rhythm showing enthusiastic traits using filler_words",
    "formality": "casual",
    "cadence": {
      "avg_sentence_length": "8.5 words",
      "rhythm_notes": "short_bursts, enthusiastic"
    },
    "punctuation": {
      "traits": ["enthusiastic", "inquisitive"]
    },
    "emoji_and_markers": {
      "emoji_frequency": "2.3%",
      "markers": ["filler_words", "abbreviations"]
    },
    "lexicon": {
      "register": "casual",
      "favorite_words": ["really", "cool", "awesome", "yeah", "like"],
      "hedges": ["maybe", "i think"],
      "intensifiers": ["really", "so", "totally"]
    },
    "moves": {
      "opens": ["greeting", "grateful"],
      "clarifies": ["seeking_clarification"],
      "redirects": ["topic_shift"],
      "boundaries": ["information_sharing"],
      "closes": ["polite_close"]
    },
    "style_rules_do": [
      "Keep language relaxed and conversational",
      "Use exclamation points to show enthusiasm",
      "Use intensifiers to emphasize points"
    ],
    "style_rules_dont": [
      "Avoid overly formal or academic language"
    ],
    "signature_phrases": ["i think", "you know"],
    "safety_note": "Analysis based on conversation patterns. Individual messages may vary."
  },
  "analysis_metadata": {
    "total_messages_analyzed": 45,
    "analysis_date": 1705312200.0,
    "analysis_version": "1.0"
  }
}
```

## Analysis Items Description

### Summary
Overall summary of user's conversation style

### Formality
- **formal**: Uses formal language
- **casual**: Uses informal language  
- **mixed**: Mix of formal/informal language

### Cadence
- **avg_sentence_length**: Average sentence length
- **rhythm_notes**: Rhythm patterns (short_bursts, long_form, ellipsis_heavy, etc.)

### Punctuation
- **enthusiastic**: Uses exclamation marks frequently
- **inquisitive**: Uses question marks frequently
- **thoughtful_pauser**: Uses ellipsis(...) frequently
- **parenthetical**: Uses parentheses frequently

### Emoji and Markers
- **emoji_frequency**: Emoji usage frequency
- **markers**: Marker patterns (filler_words, intensifiers, abbreviations, etc.)

### Lexicon
- **register**: Vocabulary level (casual, neutral, formal)
- **favorite_words**: Most frequently used words
- **hedges**: Hedging words (maybe, perhaps, I think, etc.)
- **intensifiers**: Intensifying words (really, very, so, etc.)

### Moves
- **opens**: Conversation opening patterns
- **clarifies**: Clarification patterns
- **redirects**: Topic shift patterns
- **boundaries**: Boundary setting patterns
- **closes**: Conversation closing patterns

### Style Rules
- **do**: Recommended style rules
- **dont**: Style rules to avoid

### Signature Phrases
Special phrase patterns frequently used by the user

## Notes

- The script only reads existing conversation data and does not modify it
- Analysis results are newly stored in the `user_communication_style` collection
- If analysis already exists for a user, it will be updated
- Analysis is pattern-based, so individual messages may vary
