#!/bin/bash
# Check if gemini-mac-app is running; kill and restart if so, else just start.

APP_NAME="gemini-mac-app"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

check_running() {
  pgrep -f "target/debug/$APP_NAME" > /dev/null 2>&1 || \
  pgrep -f "tauri dev" > /dev/null 2>&1
}

if check_running; then
  echo "Found running instance. Stopping..."
  PIDS=$(pgrep -f "target/debug/$APP_NAME" 2>/dev/null)
  TAURI_PIDS=$(pgrep -f "tauri dev" 2>/dev/null)
  VITE_PIDS=$(lsof -ti :1420 2>/dev/null)
  for pid in $PIDS $TAURI_PIDS $VITE_PIDS; do
    kill "$pid" 2>/dev/null && echo "  Killed PID $pid"
  done
  sleep 2
  echo "Restarting..."
else
  echo "No running instance found. Starting..."
fi

cd "$SCRIPT_DIR"
export PATH="$PATH:$HOME/.cargo/bin"
export CARGO_TARGET_DIR="$HOME/.cargo/targets/$(basename "$SCRIPT_DIR")"
npm run tauri dev
