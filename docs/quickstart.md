# Quickstart

## 1. Prepare The Runtime Sandbox

Assume the live sandbox is:

```bash
/dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto
```

The controller task writes its runtime state under:

```bash
runtime/platform_<task_id>/
```

The stable discovery files live at:

```bash
runtime/current_runtime.json
runtime/state/controller_state.json
runtime/state/controller_lease.json
```

## 2. Prepare The Tiny Submit Directory

Create a tiny submit directory and copy only:

- `controller.py`
- `volc_dispatcher_entry.sh`

Example:

```bash
mkdir -p /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto/min_submit
cp controller.py min_submit/controller.py
cp volc_dispatcher_entry.sh min_submit/volc_dispatcher_entry.sh
```

## 3. Submit The Controller Task

On `dev-intern-02`:

```bash
export PATH=/dev_vepfs/rc_wu/.volc/bin:$PATH
volc ml_task submit -c /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto/zoom_dino_volc_dispatcher_proto_20260318_meshovernight_min_1n.yaml
```

Prefer the minimal submit YAML because it avoids code upload limits.

The examples now use a `15`-day `ActiveDeadlineSeconds` so the controller can survive longer-lived occupancy waves.

## 4. Validate The Controller

Do not trust `Running` alone. Queue a smoke job and confirm queue -> done.

Example:

```bash
python submit_job.py \
  --root /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto/runtime/platform_<task_id> \
  --job-id smoke_gpu_probe \
  --workdir /dev_vepfs/rc_wu \
  --timeout-seconds 300 \
  -- bash -lc "hostname && nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader"
```

Then inspect:

- `jobs/done/smoke_gpu_probe.result.json`
- `logs/jobs/smoke_gpu_probe.log`

If the smoke moves to `done/` and the log looks sane, the controller is usable.

## 4.5 Validate Restart Adoption

Before trusting the runtime for long jobs, validate one forced restart:

1. enqueue a long-ish smoke job, for example `sleep 60`
2. stop the controller process inside the task once
3. start the controller again against the same runtime root
4. confirm:
   - the child reappears in `state/controller_state.json`
   - the same GPU slice stays occupied
   - the result still lands in `jobs/done/` after the child exits

If restart adoption fails here, do not queue overnight training yet.

## 5. Submit Real Jobs

Write one JSON spec per job into:

```bash
runtime/platform_<task_id>/jobs/queue/
```

Required fields:

- `job_id`
- `command`
- `workdir`
- `env`
- `timeout_seconds`

Optional GPU-scheduling fields:

- `gpu_indices`
- `gpu_count`
- `allowed_gpu_indices`
- `priority`

Use [sample_queue_job.json](/F:/InformationAndCourses/Code/node-controller/examples/sample_queue_job.json) as the starting shape.

Examples:

```bash
python submit_job.py \
  --root /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto/runtime/platform_<task_id> \
  --job-id train_on_01 \
  --gpu-indices 0,1 \
  --workdir /dev_vepfs/rc_wu \
  --timeout-seconds 604800 \
  -- bash -lc "echo train_on_01"
```

```bash
python submit_job.py \
  --root /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto/runtime/platform_<task_id> \
  --job-id dino_on_23 \
  --gpu-indices 2,3 \
  --workdir /dev_vepfs/rc_wu \
  --timeout-seconds 432000 \
  -- bash -lc "echo dino_on_23"
```

## 6. Observe

Use vePFS runtime files as the source of truth:

- `runtime/current_runtime.json`
- `runtime/state/controller_state.json`
- `runtime/state/controller_lease.json`
- `state/controller_state.json`
- `logs/controller_heartbeat.jsonl`
- `jobs/running/*.meta.json`
- `jobs/done/*.result.json`
- `jobs/failed/*.result.json`

Also inspect:

- `state/controller_lease.json`
  - confirms which controller pid/host currently owns the runtime root
- `jobs/running/*.meta.json`
  - confirms the child pid, start tick, and allocated GPU slice that will be re-adopted after restart

This path still works when `volc ml_task logs` is permission-blocked.

For a lightweight `nvidia-smi`-style view:

```bash
python submit_job.py \
  --root /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto \
  --status-summary
```

For watch mode:

```bash
python submit_job.py \
  --root /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto \
  --status-summary \
  --status-watch-seconds 5
```

## 7. Control The Controller Without Platform Stop Permission

If you need to stop the active child, clear abandoned queue items, or retire the whole controller:

```bash
python submit_control.py \
  --root /dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto/runtime/platform_<task_id> \
  --action retire_controller \
  --cancel-active-job \
  --purge-queue \
  --reason "replace stale forwarding controller"
```

Then inspect:

- `control/done/*.result.json`
- `control/failed/*.result.json`
