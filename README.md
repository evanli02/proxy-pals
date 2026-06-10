# Slack Chatbot for CS 5999

A Slack bot that uses OpenAI's GPT model to engage in conversations and ask questions to understand user communication patterns.

## Features

- Real-time streaming responses via HTTPS Events API
- Conversation context management
- Persistent conversation history
- Multiple agent personalities for different interaction styles

## Setup

### Environment Variables

You need to set the following environment variables on Heroku:

```bash
heroku config:set SLACK_BOT_TOKEN=xoxb-your-bot-token
heroku config:set SLACK_SIGNING_SECRET=your-signing-secret
heroku config:set OPENAI_API_KEY=sk-your-openai-key
```

### Slack App Configuration

1. Create a Slack App at https://api.slack.com/apps
2. **Disable Socket Mode** (we're using HTTPS Events API)
3. Add Bot Token Scopes:
   - `app_mentions:read`
   - `chat:write`
   - `channels:history`
   - `groups:history`
   - `im:history`
   - `mpim:history`
4. Enable Event Subscriptions:
   - Set Request URL to: `https://your-app-name.herokuapp.com/slack/events`
   - Subscribe to bot events:
     - `message.channels`
     - `message.groups`
     - `message.im`
     - `message.mpim`
5. Get your Signing Secret from Basic Information
6. Install the app to your workspace

## Deployment

```bash
# Push to Heroku
git push heroku master

# Make sure web dyno is running
heroku ps:scale web=1

# View logs
heroku logs --tail
```

## Files

- `learning_bot_service.py` - Main bot with streaming chat completions
- `streaming_test.py` - Alternative version using OpenAI Assistants
- `get_conversation.py` - Retrieve conversation history for a specific user
- `retrieve_history.py` - Retrieve conversation history from Slack API

