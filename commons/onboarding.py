from datetime import datetime

GREETING_EN = (
    "Hi! I'm an interview bot who will ask you a few questions about yourself. "
    "Please answer my questions in your own natural language and tone, reflecting how you chat with your friends "
    "(e.g., using your habitual phrases or favorite emojis, if appropriate). "
    "If you feel uncomfortable answering any questions, feel free to skip them by saying you don't want to answer.\n\n"
    "To continue with the study, please answer the questions until I say, \"You are done.\"\n\n"
    "So, are you ready to begin?"
)

FAREWELL_EN = (
    "Thank you for answering all of my questions. \"You are done!\"\n"
    "Based on your survey and interview response, we just created your clone agent.\n"
    "Please read the instructions below to test and revise your clone by yourself.\n\n"
    "*Step 1: Test Your Proxy (=Clone)*\n"
    "Install the \"Proxy Bot\" on Slack: Apps > More actions > Manage > Browse apps > search \"Proxy Bot\" and begin chatting with your clone.\n"
    "• Take some time to talk with your proxy agent (~10 min). Ask any questions that you would want to ask yourself.\n"
    "• If your proxy cannot answer a question (either too private or unclear), it will decline to respond. Those unanswered questions will appear in the next step for your review.\n\n"
    "*Step 2: Review Unanswered Questions*\n"
    "Go back to the \"Interview Bot\" app and type \"review\" in the chat. The bot will display a list of questions your proxy could not answer. For each question:\n"
    "• *Answer* it if you are comfortable with your proxy responding to it.\n"
    "• *Skip* it if the question is too private or inappropriate to be answered.\n"
    "Once finished, return to the Proxy Bot to confirm your updated answers are reflected.\n\n"
    "*Step 3: Add Information to Your Proxy (Optional)*\n"
    "If there are facts about yourself you want your proxy to represent, you can add them using the review mechanism: ask the *Proxy Bot* a question about that fact, then return to the *Interview Bot* to provide your answer. This allows you to self-design how your proxy responds to questions you care about.\n\n"
    "*Step 4: Complete a post-survey*\n"
    "After reviewing your proxy, please complete the survey: https://cornell.ca1.qualtrics.com/jfe/form/SV_dbU43xuzOuAGiBE"
)

def get_or_create_user(db, user_id):
    return db.users.find_one({"_id": user_id})

def init_user(db, user_id, meta=None):
    doc = {
        "_id": user_id,
        "created_at": datetime.utcnow(),
        "consent": None,
        "onboarding_shown": True,
        "locale": "en",
        "profile": meta or {},
    }
    db.users.insert_one(doc)
    return doc

def onboarding_message():
    return GREETING_EN

def farewell_message():
    return FAREWELL_EN
