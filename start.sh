#!/bin/sh
# Starts the hosted app. With DATA_DIR on a persistent disk, the demo data is copied there on first boot and
# kept across restarts. Without it, every restart begins again from the original demo data.
set -e
if [ -n "$DATA_DIR" ] && [ "$DATA_DIR" != "/app/data" ]; then
  mkdir -p "$DATA_DIR"
  [ -n "$(ls -A "$DATA_DIR")" ] || cp -a /app/data/. "$DATA_DIR"/
  rm -rf /app/data && ln -s "$DATA_DIR" /app/data
fi
# the server's own Slack workspace, if its tokens are set; restarted if it ever drops
if [ -n "$SLACK_BOT_TOKEN" ] && [ -n "$SLACK_APP_TOKEN" ]; then
  (while true; do python slack_bot.py || true; sleep 10; done) &
fi
exec streamlit run app.py --server.port "${PORT:-8080}" --server.address 0.0.0.0 --server.headless true
