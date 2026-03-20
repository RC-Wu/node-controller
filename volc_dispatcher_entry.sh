#!/usr/bin/env bash
set -euo pipefail

# ---- Match vePFS file permissions (MLP tasks run as root) ----
TARGET_USER="xiyu.wang"
TARGET_UID=1001

if [ "$(id -u)" = "0" ]; then
  if ! id "$TARGET_USER" &>/dev/null; then
    groupadd -g "$TARGET_UID" "$TARGET_USER"
    useradd -u "$TARGET_UID" -g "$TARGET_UID" -M -d "/dev_vepfs/" -s /bin/bash "$TARGET_USER"
  fi
  exec runuser -u "$TARGET_USER" -- "$0" "$@"
fi

SANDBOX_ROOT=/dev_vepfs/xiyu/node-controller
PYTHON_BIN="${PYTHON_BIN:-/dev_vepfs/xiyu/miniconda3/envs/vae-poc/bin/python}"
TASK_TAG="${MLP_TASK_ID:-${MLP_TASK_NAME:-}}"
if [ -z "$TASK_TAG" ]; then
  TASK_TAG="$(date +%Y%m%dT%H%M%SZ)"
fi

RUNTIME_ROOT="${RUNTIME_ROOT:-$SANDBOX_ROOT/runtime/platform_${TASK_TAG}}"
LOG_DIR="$RUNTIME_ROOT/bootstrap_logs"

mkdir -p "$SANDBOX_ROOT/runtime" "$LOG_DIR" "$RUNTIME_ROOT/jobs/queue" "$RUNTIME_ROOT/jobs/kill"
LOG_PATH="$LOG_DIR/controller_bootstrap_$(date +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$LOG_PATH") 2>&1

set -x
echo "[dispatcher] start $(date -Is)"
echo "[dispatcher] runtime_root=$RUNTIME_ROOT"
id || true
python3 --version || true
nvidia-smi -L || true

# Detect GPU count
TOTAL_GPUS=$(nvidia-smi -L 2>/dev/null | wc -l)
TOTAL_GPUS=${TOTAL_GPUS:-8}

# Queue a smoke test job (uses 1 GPU)
cat > "$RUNTIME_ROOT/jobs/queue/smoke_gpu_probe.json" <<EOF
{
  "job_id": "smoke_gpu_probe",
  "command": ["bash", "-lc", "hostname && nvidia-smi --query-gpu=index,name,memory.total,memory.used,utilization.gpu --format=csv,noheader && echo SMOKE_OK"],
  "gpus": 1,
  "workdir": "/dev_vepfs/xiyu",
  "env": {},
  "timeout_seconds": 300
}
EOF

exec "$PYTHON_BIN" -u \
  "$SANDBOX_ROOT/controller.py" \
  --root "$RUNTIME_ROOT" \
  --total-gpus "$TOTAL_GPUS" \
  --poll-seconds "${CONTROLLER_POLL_SECONDS:-2}" \
  --heartbeat-seconds "${CONTROLLER_HEARTBEAT_SECONDS:-10}"
