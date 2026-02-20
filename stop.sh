#!/bin/bash
# Stop the running gemini-mac-app dev instance

APP_NAME="gemini-mac-app"

PIDS=$(pgrep -f "target/debug/$APP_NAME" 2>/dev/null)
TAURI_PIDS=$(pgrep -f "tauri dev" 2>/dev/null)
VITE_PIDS=$(lsof -ti :1420 2>/dev/null)

ALL_PIDS="$PIDS $TAURI_PIDS $VITE_PIDS"

if [ -z "$(echo $ALL_PIDS | tr -d ' ')" ]; then
  echo "No running instance found."
  exit 0
fi

echo "Stopping $APP_NAME..."
for pid in $ALL_PIDS; do
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null
    echo "  Killed PID $pid"
  fi
done

sleep 1
echo "Done."
