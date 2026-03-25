# Public Queue Quickstart

This note is for collaborators who need to submit jobs into a live controller runtime without owning the controller process.

## Paths

Current sandbox root:

```bash
/dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto
```

Current live runtime:

```bash
/dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto/runtime/platform_t-20260325150012-5chnp
```

For normal use, prefer passing the sandbox root. `submit_job.py` will resolve the latest runtime through `runtime/current_runtime.json`.

## Check Status

```bash
python3 /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto/submit_job.py \
  --root /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto \
  --status-summary
```

## Submit A Job

Use a unique `job_id`. Do not reuse someone else's id.

Example: exact GPU slice:

```bash
python3 /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto/submit_job.py \
  --root /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto \
  --job-id my_job_2gpu \
  --gpu-indices 0,1 \
  --workdir /dev_vepfs/rc_wu \
  --timeout-seconds 86400 \
  -- bash -lc 'echo hello && nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader'
```

Example: ask the controller for any 2 free GPUs inside a safe pool:

```bash
python3 /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto/submit_job.py \
  --root /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto \
  --job-id my_job_flexible2 \
  --gpu-count 2 \
  --allowed-gpu-indices 0,1,2,3,4,5,6,7 \
  --workdir /dev_vepfs/rc_wu \
  --timeout-seconds 86400 \
  -- bash -lc 'echo flexible launch'
```

Example: pass env vars:

```bash
python3 /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto/submit_job.py \
  --root /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto \
  --job-id my_job_env \
  --gpu-indices 2,3 \
  --workdir /dev_vepfs/rc_wu \
  --timeout-seconds 86400 \
  --env WANDB_MODE=online \
  --env CUDA_DEVICE_MAX_CONNECTIONS=1 \
  -- bash -lc 'env | grep -E "WANDB|CUDA_DEVICE"'
```

## Where To Look

Queued specs:

```bash
.../runtime/platform_<task_id>/jobs/queue/
```

Job logs:

```bash
.../runtime/platform_<task_id>/logs/jobs/<job_id>.log
```

Results:

```bash
.../runtime/platform_<task_id>/jobs/done/<job_id>.result.json
.../runtime/platform_<task_id>/jobs/failed/<job_id>.result.json
.../runtime/platform_<task_id>/jobs/cancelled/<job_id>.result.json
```

## Permission Boundary

- `jobs/queue/` is public-writable for enqueue only.
- `jobs/kill/` is not public-writable.
- `control/queue/` is not public-writable.
- If you need cancellation, queue purge, or controller retirement, ask the controller owner.
