# node-controller

Queue-driven controller for a long-lived Volc custom task.

This repo packages the controller/dispatcher pattern that was used to keep a Volc 8-GPU node occupied and accept later jobs through a vePFS queue instead of post-launch SSH takeover.

## What It Does

The controller runs inside one long-lived ML Platform task and watches a shared runtime root:

- `jobs/queue/`: drop one JSON spec per job
- `jobs/running/`: controller-owned running specs and metadata
- `jobs/done/`: finished specs and result JSONs
- `jobs/failed/`: failed specs and result JSONs
- `logs/jobs/<job_id>.log`: per-job stdout/stderr
- `state/controller_state.json`: current controller heartbeat and active job

This lets you:

1. occupy a node once
2. keep the controller alive
3. submit follow-up jobs by writing JSON into the queue
4. validate progress from vePFS even when platform container logs are permission-blocked

## Repo Layout

- `controller.py`: main dispatcher loop
- `submit_job.py`: helper that writes a JSON spec into the queue
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
5. Only after the smoke reaches `done/`, start queueing real training jobs.

See [quickstart.md](/F:/InformationAndCourses/Code/node-controller/docs/quickstart.md) for exact commands.

## Why The Tiny Submit Directory Matters

The original full-sandbox submit path eventually hit ML Platform upload limits (`size/file_num exceeded limit`).

The reliable pattern is:

- keep the live sandbox on vePFS
- keep the task `Entrypoint` pointed at the absolute vePFS path
- submit from a tiny `UserCodePath` that only includes the bootstrap files

## Timeout

The historical examples were first run with `ActiveDeadlineSeconds=43200` and later changed to a much larger value for long-lived occupancy. The examples in this repo use `432000` seconds to avoid the earlier 12-hour platform stop.
