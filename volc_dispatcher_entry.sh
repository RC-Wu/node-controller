#!/usr/bin/env bash
set -euo pipefail

SANDBOX_ROOT=/dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto
PYTHON_BIN="${PYTHON_BIN:-/dev_vepfs/rc_wu/envs/ttt3r/bin/python}"
TASK_TAG="${MLP_TASK_ID:-${MLP_TASK_NAME:-}}"
if [ -z "$TASK_TAG" ]; then
  TASK_TAG="$(date +%Y%m%dT%H%M%SZ)"
fi

RUNTIME_ROOT="${RUNTIME_ROOT:-$SANDBOX_ROOT/runtime/platform_${TASK_TAG}}"
LOG_DIR="$RUNTIME_ROOT/bootstrap_logs"

mkdir -p "$SANDBOX_ROOT/runtime" "$LOG_DIR" "$RUNTIME_ROOT/jobs/queue"
LOG_PATH="$LOG_DIR/controller_bootstrap_$(date +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$LOG_PATH") 2>&1

set -x
echo "[dispatcher-proto] start $(date -Is)"
echo "[dispatcher-proto] runtime_root=$RUNTIME_ROOT"
id || true
python3 --version || true
nvidia-smi -L || true

cat > "$RUNTIME_ROOT/jobs/queue/demo_noop2.json" <<'EOF'
{
  "job_id": "demo_noop2",
  "command": [
    "python",
    "-c",
    "import time; print('demo_noop2_start', flush=True); time.sleep(2); print('demo_noop2_done', flush=True)"
  ],
  "workdir": "/dev_vepfs/rc_wu/zoom-in-render-dino-classfier",
  "env": {},
  "timeout_seconds": 30
}
EOF

exec "$PYTHON_BIN" \
  "$SANDBOX_ROOT/controller.py" \
  --root "$RUNTIME_ROOT" \
  --poll-seconds "${CONTROLLER_POLL_SECONDS:-2}" \
  --heartbeat-seconds "${CONTROLLER_HEARTBEAT_SECONDS:-10}"

