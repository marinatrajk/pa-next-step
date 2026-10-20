"""Runs Slack bots inside the web app's own server, so anyone with the link can connect their own Slack
workspace from the Connect Slack page.

Tokens pasted on that page live in this server's memory only: they're never written to disk, and a restart
disconnects them. The server's own workspace (SLACK_BOT_TOKEN / SLACK_APP_TOKEN in its settings) is connected by
serve.py at boot, in this same process. Locally, slack_bot.py can also run on its own.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

import slack_bot

MANIFEST = Path(__file__).parent / "slack" / "manifest.yml"
_lock = threading.Lock()
_bots: dict[str, dict] = {}  # Slack team id -> {"handler", "team", "bot_user", "since", "source"}


def create_app_url() -> str:
    """Slack's 'create an app from this manifest' page, with our manifest filled in."""
    return "https://api.slack.com/apps?new_app=1&manifest_yaml=" + quote(MANIFEST.read_text())


def connect(bot_token: str, app_token: str, source: str = "pasted on the Connect Slack page") -> dict:
    """Check the tokens, then open a Socket Mode connection. Raises ValueError with a plain reason."""
    bot_token, app_token = bot_token.strip(), app_token.strip()
    if not bot_token.startswith("xoxb-"):
        raise ValueError("The bot token starts with xoxb-. It's under Install App → Bot User OAuth Token.")
    if not app_token.startswith("xapp-"):
        raise ValueError("The app-level token starts with xapp-. It's under Basic Information → App-Level Tokens.")
    try:
        info = WebClient(token=bot_token).auth_test()
    except SlackApiError as e:
        raise ValueError("Slack didn't accept the bot token. Copy it again from Install App → Bot User OAuth Token "
                         f"(Slack said: {e.response.get('error', 'unknown error')}).") from e
    except Exception as e:
        raise ValueError(f"Couldn't reach Slack to check the bot token: {e}") from e
    handler = SocketModeHandler(slack_bot.build_app(bot_token), app_token)
    try:
        handler.connect()
    except Exception as e:
        raise ValueError("Slack didn't accept the app-level token. Make sure it's from this same app and has the "
                         "connections:write scope.") from e
    with _lock:
        if old := _bots.pop(info["team_id"], None):
            old["handler"].close()
        _bots[info["team_id"]] = {"handler": handler, "team": info["team"], "bot_user": info["user"],
                                  "since": datetime.now().strftime("%H:%M"), "source": source}
    return _bots[info["team_id"]]


def disconnect(team_id: str) -> None:
    with _lock:
        if bot := _bots.pop(team_id, None):
            bot["handler"].close()


def connected() -> dict[str, dict]:
    return dict(_bots)


def server_bot() -> dict | None:
    """The standalone bot started from the server's settings, if it's running."""
    if not slack_bot.STATUS_FILE.exists():
        return None
    status = json.loads(slack_bot.STATUS_FILE.read_text())
    try:
        os.kill(status["pid"], 0)
    except OSError:
        return None
    return status
