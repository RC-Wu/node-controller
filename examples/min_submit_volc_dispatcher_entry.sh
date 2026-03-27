#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SANDBOX_ROOT="${CONTROLLER_SANDBOX_ROOT:-$SCRIPT_DIR}"
CONTROLLER_RUN_AS_ROOT="${CONTROLLER_RUN_AS_ROOT:-0}"

DEFAULT_PYTHON_BIN="/dev_vepfs/rc_wu/envs/ttt3r/bin/python"
if [ -n "${PYTHON_BIN:-}" ]; then
  PYTHON_BIN="$PYTHON_BIN"
elif [ -x "$DEFAULT_PYTHON_BIN" ]; then
  PYTHON_BIN="$DEFAULT_PYTHON_BIN"
else
  PYTHON_BIN="$(command -v python3 || command -v python)"
fi

TASK_TAG="${MLP_TASK_ID:-${MLP_TASK_NAME:-}}"
if [ -z "$TASK_TAG" ]; then
  TASK_TAG="$(date +%Y%m%dT%H%M%SZ)"
fi

RUNTIME_ROOT="${RUNTIME_ROOT:-${CONTROLLER_RUNTIME_ROOT:-$SANDBOX_ROOT/runtime/platform_${TASK_TAG}}}"
LOG_DIR="$RUNTIME_ROOT/bootstrap_logs"
CONTROLLER_REQUIRED_MOUNTS="${CONTROLLER_REQUIRED_MOUNTS:-}"

mkdir -p "$SANDBOX_ROOT/runtime" "$LOG_DIR" "$RUNTIME_ROOT/jobs/queue"
LOG_PATH="$LOG_DIR/controller_bootstrap_$(date +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$LOG_PATH") 2>&1

verify_required_mounts() {
  if [ -z "$CONTROLLER_REQUIRED_MOUNTS" ]; then
    return 0
  fi
  local mount_path=""
  for mount_path in ${CONTROLLER_REQUIRED_MOUNTS//,/ }; do
    if [ ! -e "$mount_path" ]; then
      echo "[dispatcher-proto] missing required mount path: $mount_path" >&2
      return 1
    fi
    if command -v mountpoint >/dev/null 2>&1 && ! mountpoint -q "$mount_path"; then
      echo "[dispatcher-proto] required path exists but is not a mountpoint: $mount_path" >&2
      mount | grep -F " $mount_path " || true
      return 1
    fi
  done
}

set -x
echo "[dispatcher-proto] start $(date -Is)"
echo "[dispatcher-proto] runtime_root=$RUNTIME_ROOT"
echo "[dispatcher-proto] run_as_root=$CONTROLLER_RUN_AS_ROOT"
verify_required_mounts
id || true
python3 --version || true
nvidia-smi -L || true

exec "$PYTHON_BIN" \
  "$SANDBOX_ROOT/controller.py" \
  --root "$RUNTIME_ROOT" \
  --poll-seconds "${CONTROLLER_POLL_SECONDS:-2}" \
  --heartbeat-seconds "${CONTROLLER_HEARTBEAT_SECONDS:-10}"
