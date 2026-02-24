#!/bin/bash
# Restart local tauri dev app safely without leaving stale processes.

APP_NAME="gemini-mac-app"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_CARGO_MANIFEST="$SCRIPT_DIR/src-tauri/Cargo.toml"
APP_DEBUG_PATH="$HOME/.cargo/targets/$(basename "$SCRIPT_DIR")/debug/$APP_NAME"

collect_pids() {
  {
    pgrep -f "$APP_DEBUG_PATH" 2>/dev/null
    pgrep -f "/target/debug/$APP_NAME" 2>/dev/null
    pgrep -f "$APP_CARGO_MANIFEST" 2>/dev/null
    pgrep -f "tauri dev" 2>/dev/null
    lsof -ti tcp:1420 2>/dev/null
  } | awk -v self="$$" 'NF && $1 != self' | sort -u
}

stop_running() {
  local pids left
  pids="$(collect_pids)"
  if [ -z "$pids" ]; then
    return 0
  fi

  echo "Found running instance. Stopping..."
  for pid in $pids; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null && echo "  Sent TERM to PID $pid"
    fi
  done

  for _ in 1 2 3 4 5; do
    sleep 0.2
    left="$(collect_pids)"
    if [ -z "$left" ]; then
      return 0
    fi
  done

  for pid in $left; do
    kill -9 "$pid" 2>/dev/null && echo "  Sent KILL to PID $pid"
  done
}

if [ -n "$(collect_pids)" ]; then
  stop_running
  echo "Restarting..."
else
  echo "No running instance found. Starting..."
fi

cd "$SCRIPT_DIR"
export PATH="$PATH:$HOME/.cargo/bin"
export CARGO_TARGET_DIR="$HOME/.cargo/targets/$(basename "$SCRIPT_DIR")"
npm run tauri dev
