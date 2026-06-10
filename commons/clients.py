"""
Shared client initialization for Slack and OpenAI.

Provides factory functions so each bot doesn't have to repeat the same
load_dotenv -> os.environ -> WebClient -> auth_test -> OpenAI dance.
"""

import os
import logging
from typing import Tuple

from dotenv import load_dotenv
from openai import OpenAI
from slack_sdk import WebClient

log = logging.getLogger("commons.clients")

# load .env once at import time
load_dotenv()


def make_slack_client(token_env: str = "SLACK_BOT_TOKEN") -> Tuple[WebClient, str]:
    """Create a Slack WebClient and return (client, bot_user_id).

    Args:
        token_env: name of the environment variable holding the bot token.
    """
    token = os.environ.get(token_env)
    if not token:
        raise RuntimeError(f"{token_env} not set")
    client = WebClient(token=token)
    bot_user_id = client.auth_test()["user_id"]
    log.info(f"Slack client connected as {bot_user_id} (token from {token_env})")
    return client, bot_user_id


def make_openai_client(api_key_env: str = "OPENAI_API_KEY") -> OpenAI:
    """Create an OpenAI client.

    Args:
        api_key_env: name of the environment variable holding the API key.
    """
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(f"{api_key_env} not set")
    return OpenAI(api_key=api_key)


def get_signing_secret(env_var: str = "SLACK_SIGNING_SECRET") -> str:
    """Read a Slack signing secret from the environment."""
    return os.environ.get(env_var, "")


def get_openai_model(env_var: str = "PROXY_MODEL", default: str = "gpt-5-mini") -> str:
    """Read the OpenAI model name from the environment."""
    return os.environ.get(env_var, default)
