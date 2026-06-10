# Conversation Analysis API Usage

You can execute conversation style analysis through the deployed bot on the server.

## 🚀 API Endpoints

### Execute Conversation Analysis
**POST** `/analyze-conversations`

Analyzes conversation styles for all users.

```bash
curl -X POST https://your-bot-url.herokuapp.com/analyze-conversations
```

**Response Example:**
```json
{
  "status": "success",
  "message": "Conversation analysis completed",
  "total_users_analyzed": 15,
  "collection": "user_communication_style"
}
```

## 🔧 Usage

### 1. Execute Analysis
```bash
# Start analysis
curl -X POST https://your-bot-url.herokuapp.com/analyze-conversations
```

### 2. Check in Browser
- Execute analysis: `https://your-bot-url.herokuapp.com/analyze-conversations` (POST request)

## 📊 Analysis Results

Analysis results are stored in MongoDB's `user_communication_style` collection.

### Communication Style Fields:
- **summary**: User style summary
- **formality**: Formality level (formal/casual/mixed)
- **cadence**: Rhythm and sentence length
- **punctuation**: Punctuation usage patterns
- **emoji_and_markers**: Emoji and markers
- **lexicon**: Vocabulary analysis
- **moves**: Conversation move patterns
- **style_rules_do**: Recommended style rules
- **style_rules_dont**: Style rules to avoid
- **signature_phrases**: Frequently used phrases

## ⚠️ Notes

- Analysis may take time (depending on number of users and messages)
- Do not send other requests during analysis
- Results are stored in the `user_communication_style` collection
- If analysis already exists for a user, it will be updated

## 🎯 Usage Example

```bash
# Execute analysis
curl -X POST https://your-bot-url.herokuapp.com/analyze-conversations
```
