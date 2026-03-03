#!/bin/bash
# Stop the running gemini-collector dev instance (idempotent).

APP_NAME="gemini-collector"
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

PIDS="$(collect_pids)"
if [ -z "$PIDS" ]; then
  echo "Instance already stopped."
  exit 0
fi

echo "Stopping $APP_NAME..."
for pid in $PIDS; do
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null && echo "  Sent TERM to PID $pid"
  fi
done

# Wait briefly for graceful shutdown.
for _ in 1 2 3 4 5; do
  sleep 0.2
  LEFT="$(collect_pids)"
  if [ -z "$LEFT" ]; then
    echo "Done."
    exit 0
  fi
done

# Force kill leftovers to avoid dangling dev ports/processes.
for pid in $LEFT; do
  kill -9 "$pid" 2>/dev/null && echo "  Sent KILL to PID $pid"
done

echo "Done."
