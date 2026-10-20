#!/bin/sh
# Starts the hosted app. With DATA_DIR on a persistent disk, the demo data is copied there on first boot and
# kept across restarts. Without it, every restart begins again from the original demo data.
set -e
if [ -n "$DATA_DIR" ] && [ "$DATA_DIR" != "/app/data" ]; then
  mkdir -p "$DATA_DIR"
  [ -n "$(ls -A "$DATA_DIR")" ] || cp -a /app/data/. "$DATA_DIR"/
  rm -rf /app/data && ln -s "$DATA_DIR" /app/data
fi
# the web app and the Slack bot (when its tokens are set) share one process, and one copy of the embedding model
exec python serve.py
