"""Hosted entry point: the web app and the Slack bot in one process.

    python serve.py      (start.sh runs this in the container)

One process means one copy of everything both of them load, above all the embedding model behind the knowledge base
and /investigate (about 250 MB). Two processes needed roughly twice that and went past a 512 MB instance.
The bot connects first, from SLACK_BOT_TOKEN / SLACK_APP_TOKEN when they're set, then Streamlit takes over the
main thread. The Connect Slack page shows this bot alongside any workspace connected there.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    if os.environ.get("SLACK_BOT_TOKEN") and os.environ.get("SLACK_APP_TOKEN"):
        import slack_connect
        try:
            bot = slack_connect.connect(os.environ["SLACK_BOT_TOKEN"], os.environ["SLACK_APP_TOKEN"], source="the server's settings")
            print(f"Slack bot connected to {bot['team']} as @{bot['bot_user']}", flush=True)
        except ValueError as e:
            print(f"Slack bot not started: {e}", flush=True)
    from streamlit.web import cli
    sys.argv = ["streamlit", "run", "app.py", "--server.port", os.environ.get("PORT", "8080"),
                "--server.address", "0.0.0.0", "--server.headless", "true"]
    cli.main()


if __name__ == "__main__":
    main()
