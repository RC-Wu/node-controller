# Runtime Layout

The controller expects one runtime root per task:

```text
runtime/platform_<task_id>/
|- jobs/
|  |- queue/
|  |- running/
|  |- done/
|  |- failed/
|  |- cancelled/
|  `- kill/
|- control/
|  |- queue/
|  |- done/
|  `- failed/
|- logs/
|  |- controller_heartbeat.jsonl
|  `- jobs/
`- state/
   |- controller_state.json
   `- controller_lease.json
```

## Semantics

- `queue/*.json`
  - externally written job specs
- `running/*.json`
  - controller-owned active job specs
- `running/*.meta.json`
  - active child pid, command, timeout, and assigned GPU slice
- `done/*.result.json`
  - successful completion record
- `failed/*.result.json`
  - failed completion record
- `cancelled/*.result.json`
  - queued job cancelled before launch
- `kill/<job_id>.json`
  - compatibility kill signal for queued or running jobs
- `control/queue/*.json`
  - admin requests such as `cancel_active_job`, `purge_queue`, `retire_controller`
- `control/done/*.result.json`
  - successful or accepted admin requests
- `control/failed/*.result.json`
  - rejected admin requests
- `logs/jobs/<job_id>.log`
  - child stdout/stderr
- `state/controller_state.json`
  - latest controller heartbeat, `active_jobs`, current GPU occupancy, and `gpu_status`
- `state/controller_lease.json`
  - runtime-root ownership lease for restart-safe single-controller operation
- `logs/controller_heartbeat.jsonl`
  - append-only heartbeat and job transition events

## Queue Permissions

The controller attempts to set `jobs/queue` to sticky world-writable so an external dev-machine user can enqueue jobs through vePFS without taking ownership of the rest of the runtime tree.

## Sandbox-Level Stable Paths

Outside the task-specific runtime root, the sandbox also carries stable discovery files:

```text
<sandbox_root>/runtime/current_runtime.json
<sandbox_root>/runtime/state/controller_state.json
<sandbox_root>/runtime/state/controller_lease.json
```

- `current_runtime.json`
  - controller-owned pointer to the active `platform_<task_id>` runtime
- `runtime/state/controller_state.json`
  - mirrored latest controller heartbeat for callers that only know the sandbox root
- `runtime/state/controller_lease.json`
  - mirrored latest lease for the currently published runtime
