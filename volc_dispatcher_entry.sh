#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SANDBOX_ROOT="${CONTROLLER_SANDBOX_ROOT:-$SCRIPT_DIR}"
CODE_ROOT="${CONTROLLER_CODE_ROOT:-$SCRIPT_DIR}"
CONTROLLER_RUN_AS_ROOT="${CONTROLLER_RUN_AS_ROOT:-0}"

if [ "$CONTROLLER_RUN_AS_ROOT" != "1" ] && [ "${CONTROLLER_DROPPED_PRIVS:-0}" != "1" ] && [ "$(id -u)" = "0" ] && [ -d "$SANDBOX_ROOT" ]; then
  target_uid="$(stat -c '%u' "$SANDBOX_ROOT" 2>/dev/null || echo 0)"
  target_gid="$(stat -c '%g' "$SANDBOX_ROOT" 2>/dev/null || echo 0)"
  if [ "$target_uid" != "0" ] && [ "$target_gid" != "0" ]; then
    target_group="$(getent group "$target_gid" | cut -d: -f1 || true)"
    if [ -z "$target_group" ]; then
      target_group="controller_gid_${target_gid}"
      groupadd -g "$target_gid" "$target_group" >/dev/null 2>&1 || target_group="$(getent group "$target_gid" | cut -d: -f1)"
    fi
    target_user="$(getent passwd "$target_uid" | cut -d: -f1 || true)"
    if [ -z "$target_user" ]; then
      target_user="controller_uid_${target_uid}"
      useradd -u "$target_uid" -g "$target_group" -M -d "$SANDBOX_ROOT" -s /bin/bash "$target_user" >/dev/null 2>&1 || target_user="$(getent passwd "$target_uid" | cut -d: -f1)"
    fi
    if [ -n "$target_user" ] && command -v runuser >/dev/null 2>&1; then
      export CONTROLLER_DROPPED_PRIVS=1
      exec runuser -u "$target_user" -- "$0" "$@"
    fi
  fi
fi

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
SUPERVISOR_STATUS_PATH="$RUNTIME_ROOT/state/controller_supervisor.json"
CURRENT_RUNTIME_POINTER="$SANDBOX_ROOT/runtime/current_runtime.json"
AUTO_RESTART="${CONTROLLER_AUTO_RESTART:-1}"
MAX_RESTARTS="${CONTROLLER_MAX_RESTARTS:-0}"
RESTART_DELAY_SECONDS="${CONTROLLER_RESTART_DELAY_SECONDS:-5}"
CONTROLLER_REQUIRED_MOUNTS="${CONTROLLER_REQUIRED_MOUNTS:-}"
PUBLIC_QUEUE_HOME="${PUBLIC_QUEUE_HOME:-/dev_vepfs/rc_wu}"

umask 022

mkdir -p "$SANDBOX_ROOT/runtime" "$LOG_DIR" "$RUNTIME_ROOT/jobs/queue" "$RUNTIME_ROOT/state"
LOG_PATH="$LOG_DIR/controller_bootstrap_$(date +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$LOG_PATH") 2>&1

ensure_public_queue_access() {
  local stop_dir="$PUBLIC_QUEUE_HOME"
  local dir="$RUNTIME_ROOT"
  while [ -n "$dir" ] && [ "$dir" != "/" ]; do
    if [ -d "$dir" ]; then
      chmod 755 "$dir" >/dev/null 2>&1 || true
    fi
    if [ "$dir" = "$stop_dir" ]; then
      break
    fi
    dir="$(dirname "$dir")"
  done

  for dir in \
    "$LOG_DIR" \
    "$RUNTIME_ROOT/jobs" \
    "$RUNTIME_ROOT/logs" \
    "$RUNTIME_ROOT/state" \
    "$RUNTIME_ROOT/control" \
    "$RUNTIME_ROOT/control/queue" \
    "$RUNTIME_ROOT/control/done" \
    "$RUNTIME_ROOT/control/failed" \
    "$RUNTIME_ROOT/jobs/kill" \
    "$RUNTIME_ROOT/jobs/running" \
    "$RUNTIME_ROOT/jobs/done" \
    "$RUNTIME_ROOT/jobs/failed" \
    "$RUNTIME_ROOT/jobs/cancelled"; do
    if [ -d "$dir" ]; then
      chmod 755 "$dir" >/dev/null 2>&1 || true
    fi
  done

  if [ -d "$RUNTIME_ROOT/jobs/queue" ]; then
    chmod 1777 "$RUNTIME_ROOT/jobs/queue" >/dev/null 2>&1 || true
  fi
  if [ -d "$RUNTIME_ROOT/control/queue" ]; then
    chmod 1777 "$RUNTIME_ROOT/control/queue" >/dev/null 2>&1 || true
  fi
  if [ -d "$RUNTIME_ROOT/jobs/kill" ]; then
    chmod 1777 "$RUNTIME_ROOT/jobs/kill" >/dev/null 2>&1 || true
  fi
  if [ -e "$SUPERVISOR_STATUS_PATH" ]; then
    chmod 644 "$SUPERVISOR_STATUS_PATH" >/dev/null 2>&1 || true
  fi
  if [ -e "$CURRENT_RUNTIME_POINTER" ]; then
    chmod 644 "$CURRENT_RUNTIME_POINTER" >/dev/null 2>&1 || true
  fi
}

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

write_supervisor_status() {
  "$PYTHON_BIN" - "$SUPERVISOR_STATUS_PATH" "$CURRENT_RUNTIME_POINTER" "$RUNTIME_ROOT" "$SANDBOX_ROOT" "$TASK_TAG" "$1" "$2" "$3" "$AUTO_RESTART" "$MAX_RESTARTS" "$RESTART_DELAY_SECONDS" <<'PY'
import json
import os
import socket
import sys
import time
from pathlib import Path

status_path = Path(sys.argv[1])
pointer_path = Path(sys.argv[2])
runtime_root = sys.argv[3]
sandbox_root = sys.argv[4]
task_tag = sys.argv[5]
state = sys.argv[6]
last_exit_code = sys.argv[7]
restart_count = int(sys.argv[8])
auto_restart = sys.argv[9] == "1"
max_restarts = int(sys.argv[10])
restart_delay_seconds = float(sys.argv[11])

payload = {
    "schema_version": 2,
    "updated_at_epoch": time.time(),
    "hostname": socket.gethostname(),
    "supervisor_pid": os.getpid(),
    "runtime_root": runtime_root,
    "runtime_name": Path(runtime_root).name,
    "sandbox_root": sandbox_root,
    "task_tag": task_tag,
    "state": state,
    "last_exit_code": None if last_exit_code == "" else int(last_exit_code),
    "restart_count": restart_count,
    "auto_restart": auto_restart,
    "max_restarts": max_restarts,
    "restart_delay_seconds": restart_delay_seconds,
}
status_path.parent.mkdir(parents=True, exist_ok=True)
tmp = status_path.with_suffix(status_path.suffix + ".tmp")
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp.replace(status_path)

pointer_payload = {
    "schema_version": 2,
    "updated_at_epoch": time.time(),
    "runtime_root": runtime_root,
    "runtime_name": Path(runtime_root).name,
    "sandbox_root": sandbox_root,
    "task_tag": task_tag,
    "hostname": socket.gethostname(),
    "state": state,
    "supervisor_status_path": str(status_path),
    "controller_state_path": str(Path(runtime_root) / "state" / "controller_state.json"),
    "controller_lease_path": str(Path(runtime_root) / "state" / "controller_lease.json"),
    "controller_heartbeat_path": str(Path(runtime_root) / "logs" / "controller_heartbeat.jsonl"),
}
pointer_path.parent.mkdir(parents=True, exist_ok=True)
pointer_tmp = pointer_path.with_suffix(pointer_path.suffix + ".tmp")
pointer_tmp.write_text(json.dumps(pointer_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
pointer_tmp.replace(pointer_path)
PY
}

set -x
echo "[dispatcher-proto] start $(date -Is)"
echo "[dispatcher-proto] runtime_root=$RUNTIME_ROOT"
echo "[dispatcher-proto] run_as_root=$CONTROLLER_RUN_AS_ROOT"
ensure_public_queue_access
verify_required_mounts
id || true
python3 --version || true
nvidia-smi -L || true

restart_count=0
while true; do
  write_supervisor_status "starting_controller" "" "$restart_count"
  ensure_public_queue_access
  set +e
  "$PYTHON_BIN" \
    "$CODE_ROOT/controller.py" \
    --root "$RUNTIME_ROOT" \
    --poll-seconds "${CONTROLLER_POLL_SECONDS:-2}" \
    --heartbeat-seconds "${CONTROLLER_HEARTBEAT_SECONDS:-10}" \
    --foreign-gpu-memory-threshold-mb "${CONTROLLER_FOREIGN_GPU_MEMORY_THRESHOLD_MB:-2048}" \
    --startup-min-schedulable-gpu-count "${CONTROLLER_STARTUP_MIN_SCHEDULABLE_GPUS:-0}"
  rc=$?
  set -e

  write_supervisor_status "controller_exited" "$rc" "$restart_count"
  ensure_public_queue_access
  if [ "$rc" -eq 2 ]; then
    write_supervisor_status "startup_unhealthy" "$rc" "$restart_count"
    ensure_public_queue_access
    exit "$rc"
  fi
  if [ "$rc" -eq 0 ] || [ "$AUTO_RESTART" != "1" ]; then
    write_supervisor_status "stopped" "$rc" "$restart_count"
    ensure_public_queue_access
    exit "$rc"
  fi

  restart_count=$((restart_count + 1))
  if [ "$MAX_RESTARTS" -gt 0 ] && [ "$restart_count" -gt "$MAX_RESTARTS" ]; then
    write_supervisor_status "restart_limit_reached" "$rc" "$restart_count"
    ensure_public_queue_access
    exit "$rc"
  fi

  write_supervisor_status "sleep_before_restart" "$rc" "$restart_count"
  ensure_public_queue_access
  sleep "$RESTART_DELAY_SECONDS"
done
