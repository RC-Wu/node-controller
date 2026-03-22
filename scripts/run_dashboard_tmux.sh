#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: $0 <sandbox-root> [port]" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SANDBOX_ROOT="$1"
PORT="${2:-8787}"
SESSION_NAME="${SESSION_NAME:-node-controller-dashboard}"
HOST="${HOST:-127.0.0.1}"
ALLOW_WRITE_ACTIONS="${ALLOW_WRITE_ACTIONS:-0}"
LOG_DIR="${LOG_DIR:-$SANDBOX_ROOT/runtime/host_services/dashboard}"

mkdir -p "$LOG_DIR"
LOG_PATH="$LOG_DIR/dashboard_$(date +%Y%m%dT%H%M%S).log"

WRITE_FLAG=""
if [ "$ALLOW_WRITE_ACTIONS" = "1" ]; then
  WRITE_FLAG="--allow-write-actions"
fi

CMD="cd '$REPO_ROOT' && python3 dashboard_server.py --root '$SANDBOX_ROOT' --host '$HOST' --port '$PORT' $WRITE_FLAG 2>&1 | tee -a '$LOG_PATH'"

tmux has-session -t "$SESSION_NAME" 2>/dev/null && tmux kill-session -t "$SESSION_NAME"
tmux new-session -d -s "$SESSION_NAME" "$CMD"

echo "session=$SESSION_NAME"
echo "url=http://$HOST:$PORT"
echo "log=$LOG_PATH"
