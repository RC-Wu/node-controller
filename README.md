# node-controller

Queue-driven controller for a long-lived Volc custom task.

This repo packages the controller/dispatcher pattern that was used to keep a Volc 8-GPU node occupied and accept later jobs through a vePFS queue instead of post-launch SSH takeover.

## Current Architecture

- `controller.py` stays focused on queue polling, GPU reservation, control requests, lease management, and runtime snapshots.
- `job_supervisor.py` is now the per-job execution boundary.
  - the controller no longer launches training commands directly
  - job crashes do not directly crash the controller process
  - each job gets its own structured event log plus stdout/stderr log
- long-running operator-side services should run on `dev-intern-01/02`, not on the PC:
  - the actual training controller lives inside the long-lived Volc task
  - the host-side dashboard / task supervisor can live in `tmux` on the dev machine
  - host-side logs should still point at vePFS so PC shutdown does not matter

## Audit And Safety Changes

- `submit_job.py --kill` now routes through the audited `control/queue/` path instead of writing an anonymous file under `jobs/kill/`.
- control requests now include origin metadata such as:
  - `requested_by`
  - `requested_from_host`
  - `requested_from_pid`
  - `request_source`
- the controller writes:
  - `logs/controller.log`
  - `logs/controller_events.jsonl`
  - `logs/controller_admin_audit.jsonl`
- each job supervisor writes:
  - `logs/jobs/<job_id>.log`
  - `logs/jobs/<job_id>.events.jsonl`

The legacy `jobs/kill/` compatibility path is now disabled by default and must be explicitly re-enabled with `--enable-compat-kill-queue`.

## What It Does

The controller runs inside one long-lived ML Platform task and watches a shared runtime root:

- `jobs/queue/`: drop one JSON spec per job
- `jobs/running/`: controller-owned running specs and metadata
- `jobs/done/`: finished specs and result JSONs
- `jobs/failed/`: failed specs and result JSONs
- `jobs/cancelled/`: queue specs that were cancelled before launch
- `logs/jobs/<job_id>.log`: per-job stdout/stderr
- `state/controller_state.json`: current controller heartbeat and active job
- `runtime/current_runtime.json`: stable runtime pointer for the currently active controller task
- `state/controller_supervisor.json`: in-task supervisor state for controller auto-restart
- `control/queue/`: externally written admin requests
- `control/done/`: completed admin requests
- `control/failed/`: rejected admin requests

This lets you:

1. occupy a node once
2. keep the controller alive
3. submit follow-up jobs by writing JSON into the queue
4. run multiple independent jobs concurrently when their GPU slices do not overlap
5. validate progress from vePFS even when platform container logs are permission-blocked
6. restart the controller process and re-adopt existing `jobs/running/*.meta.json` children instead of losing GPU occupancy
7. prevent two controllers from accidentally managing the same runtime root through a runtime lease
8. auto-restart `controller.py` inside the task entrypoint on non-zero exit
9. query a human-readable summary from the sandbox root instead of hardcoding `platform_<task_id>`

## GPU Slice Aware Scheduling

The controller now understands optional job-spec fields:

- `gpu_indices`
  - exact GPU slice to reserve, e.g. `["0", "1"]`
- `gpu_count`
  - number of GPUs to allocate if no exact slice is given
- `allowed_gpu_indices`
  - eligible GPU pool for first-fit allocation
- `priority`
  - higher values are considered earlier when multiple queued jobs fit

If a job requests no GPUs, it is treated as CPU/non-exclusive work.

## Restart And Lease Behavior

- On startup, the controller scans `jobs/running/*.json` plus matching `*.meta.json` and re-attaches still-alive child processes.
- Re-attached jobs keep their reserved GPU slices, so a controller restart does not immediately free those GPUs to new queue items.
- The runtime root now carries a controller lease under `state/controller_lease.json` plus `state/controller.lock/`.
- A second controller pointed at the same runtime root will fail fast instead of racing the first one.
- If the lease is stale, the next controller instance can reclaim it and continue.
- `volc_dispatcher_entry.sh` now supervises `controller.py` and restarts it on non-zero exit while keeping the same runtime root.
- The parent `runtime/` directory mirrors stable discovery at:
  - `runtime/current_runtime.json`
  - `runtime/state/controller_state.json`
  - `runtime/state/controller_lease.json`

## Repo Layout

- `controller.py`: main dispatcher loop
- `job_supervisor.py`: isolated per-job runner used by the controller
- `runtime_io.py`: shared runtime resolution and JSON helpers used by tooling and dashboard code
- `submit_job.py`: helper that writes a JSON spec into the queue
- `submit_job.py --status-summary`: human-readable controller and GPU occupancy view
- `submit_control.py`: helper that writes an admin request into `control/queue`
- `dashboard_server.py`: stdlib HTTP server that exposes summary/history/jobs/logs APIs and serves the static dashboard
- `dashboard/static/`: no-build frontend for operators
- `scripts/run_dashboard_tmux.sh`: dev-machine tmux launcher for the dashboard
- `scripts/run_controller_supervisor_tmux.sh`: dev-machine tmux launcher for the host-side controller supervisor
- `volc_dispatcher_entry.sh`: task entrypoint that builds the runtime root and starts the controller
- `examples/controller_task_min_submit.yaml`: minimal ML task submit config using a tiny `UserCodePath`
- `examples/controller_task_full_submit.yaml`: historical full-sandbox submit config
- `examples/min_submit_controller.py`: example file set for the tiny submit directory
- `examples/min_submit_volc_dispatcher_entry.sh`: example file set for the tiny submit directory
- `examples/sample_queue_job.json`: minimal job spec example
- `docs/quickstart.md`: end-to-end usage on `dev-intern-02`
- `docs/runtime-layout.md`: runtime file layout and semantics
- `docs/operations.md`: practical caveats and recovery notes

## Fast Start

1. Prepare a tiny submit directory that contains only:
   - `controller.py`
   - `volc_dispatcher_entry.sh`
2. Submit a long-lived task with `examples/controller_task_min_submit.yaml`.
3. Wait until the task is `Running`.
4. Validate the controller by queueing a tiny smoke job.
5. Restart the controller once on purpose, verify it re-adopts the smoke job or an equivalent long-running child, and only then trust it for overnight work.
6. Only after the smoke reaches `done/`, start queueing real training jobs.
7. If you need to stop an active child, purge abandoned queue items, or retire the controller without ML Platform stop permission, write an admin request into `control/queue/`.

See [quickstart.md](/F:/InformationAndCourses/Code/node-controller/docs/quickstart.md) for exact commands.

## Fast Status Query

Use the sandbox root directly:

```bash
python submit_job.py \
  --root /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto \
  --status-summary
```

Watch it like `nvidia-smi`:

```bash
python submit_job.py \
  --root /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto \
  --status-summary \
  --status-watch-seconds 5
```

The summary includes the current runtime root, job counters, active controller jobs, per-GPU utilization, and live compute processes annotated with controller job ids when they match a managed process group.

For the richer browser UI:

```bash
python dashboard_server.py \
  --root /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto \
  --host 127.0.0.1 \
  --port 8787
```

Then open `http://127.0.0.1:8787`.

For dev-machine background use:

```bash
bash scripts/run_dashboard_tmux.sh /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto 8787
```

## Why The Tiny Submit Directory Matters

The original full-sandbox submit path eventually hit ML Platform upload limits (`size/file_num exceeded limit`).

The reliable pattern is:

- keep the live sandbox on vePFS
- keep the task `Entrypoint` pointed at the absolute vePFS path
- submit from a tiny `UserCodePath` that only includes the bootstrap files

## Timeout

The historical examples were first run with `ActiveDeadlineSeconds=43200` and later changed to a much larger value for long-lived occupancy. The examples in this repo now use `1296000` seconds (`15` days).

## Discovery And Status

- Stable discovery path:
  - `runtime/current_runtime.json`
- Mirrored controller state:
  - `runtime/state/controller_state.json`
  - `runtime/state/controller_lease.json`
- Raw runtime-local heartbeat:
  - `runtime/platform_<task_id>/state/controller_state.json`
