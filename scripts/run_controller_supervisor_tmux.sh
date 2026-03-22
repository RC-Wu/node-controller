#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 2 ]; then
  echo "usage: $0 <sandbox-root> <submit-config>" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SANDBOX_ROOT="$1"
SUBMIT_CONFIG="$2"
SESSION_NAME="${SESSION_NAME:-node-controller-supervisor}"
LOG_DIR="${LOG_DIR:-$SANDBOX_ROOT/runtime/host_services/supervisor}"
POLL_SECONDS="${POLL_SECONDS:-30}"
HEALTHY_MAX_AGE_SECONDS="${HEALTHY_MAX_AGE_SECONDS:-45}"
VOLC_BINARY="${VOLC_BINARY:-volc}"

mkdir -p "$LOG_DIR"
LOG_PATH="$LOG_DIR/supervisor_$(date +%Y%m%dT%H%M%S).log"

CMD="cd '$REPO_ROOT' && python3 scripts/controller_supervisor.py --sandbox-root '$SANDBOX_ROOT' --submit-config '$SUBMIT_CONFIG' --volc-binary '$VOLC_BINARY' --poll-seconds '$POLL_SECONDS' --healthy-max-age-seconds '$HEALTHY_MAX_AGE_SECONDS' 2>&1 | tee -a '$LOG_PATH'"

tmux has-session -t "$SESSION_NAME" 2>/dev/null && tmux kill-session -t "$SESSION_NAME"
tmux new-session -d -s "$SESSION_NAME" "$CMD"

echo "session=$SESSION_NAME"
echo "log=$LOG_PATH"
