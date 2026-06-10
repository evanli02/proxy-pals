"""
Unified WSGI entrypoint that serves both bot and proxy_bot endpoints
under a single process. It dispatches based on URL prefixes:

- Paths starting with '/slack/proxy' or '/proxy' go to proxy_bot.app
- All other paths go to bot.app

This lets both Slack endpoints work together on one dyno/port without
modifying existing route definitions.
"""
import logging

from proxy_bot.proxy_controller import app as proxy_app
from learning_bot.learning_bot_service import app as bot_app
from validation_bot.validation_controller import app as validation_app

log = logging.getLogger("combined_app")


def application(environ, start_response):
    path: str = environ.get("PATH_INFO", "") or ""
    # Route to proxy for its prefixed endpoints; everything else to bot
    if path.startswith("/proxy"):
        return proxy_app(environ, start_response)
    if path.startswith("/validation"):
        return validation_app(environ, start_response)
    return bot_app(environ, start_response)
