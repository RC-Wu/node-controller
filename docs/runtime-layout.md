# Runtime Layout

The controller expects one runtime root per task:

```text
runtime/platform_<task_id>/
|- jobs/
|  |- queue/
|  |- running/
|  |- done/
|  |- failed/
|  `- cancelled/
|- control/
|  |- queue/
|  |- done/
|  `- failed/
|- logs/
|  |- controller_heartbeat.jsonl
|  `- jobs/
`- state/
   `- controller_state.json
```

## Semantics

- `queue/*.json`
  - externally written job specs
- `running/*.json`
  - controller-owned active job specs
- `running/*.meta.json`
  - active child pid, command, log path, timeout
- `done/*.result.json`
  - successful completion record
- `failed/*.result.json`
  - failed completion record
- `cancelled/*.result.json`
  - queued job cancelled before launch
- `control/queue/*.json`
  - admin requests such as `cancel_active_job`, `purge_queue`, `retire_controller`
- `control/done/*.result.json`
  - successful or accepted admin requests
- `control/failed/*.result.json`
  - rejected admin requests
- `logs/jobs/<job_id>.log`
  - child stdout/stderr
- `state/controller_state.json`
  - latest controller heartbeat and current active job
- `logs/controller_heartbeat.jsonl`
  - append-only heartbeat and job transition events

## Queue Permissions

The controller attempts to set `jobs/queue` to sticky world-writable so an external dev-machine user can enqueue jobs through vePFS without taking ownership of the rest of the runtime tree.
