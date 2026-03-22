# Operations Notes

## Upload Limit Workaround

If `volc ml_task submit` fails with:

```text
upload code error: the size/file_num exceeded limit
```

do not keep submitting from the full live sandbox. Switch to the tiny submit directory pattern and leave `Entrypoint` pointing at the absolute vePFS path.

## Log Permission Caveat

Direct ML Platform container logs may be permission-blocked for the current account. That does not mean the controller is dead.

Preferred validation:

1. enqueue a tiny smoke job
2. wait for `jobs/done/<job_id>.result.json`
3. inspect `logs/jobs/<job_id>.log`

## Shared Queue Hygiene

Before queueing a new real training job, inspect:

- `state/controller_state.json`
- `jobs/queue/`
- `jobs/running/`

Do not blindly submit duplicate work if another automation or worker already enqueued an equivalent spec.

## Slice Aware Concurrency

- The scheduler is opportunistic first-fit over the queued specs, not strict FIFO.
- If the first queued job wants `0,1,2,3` but only `4,5,6,7` are free, a later smaller job may still launch first if its slice fits.
- For deterministic placement, set explicit `gpu_indices`.
- For flexible placement inside a safe pool, set `gpu_count` plus `allowed_gpu_indices`.

## Restart Adoption

- The controller now treats `jobs/running/*.meta.json` as restart state, not just debug artifacts.
- If the controller process dies but the child pid is still alive and the command still matches, the next controller instance re-attaches it on boot.
- Validation rule:
  - do at least one deliberate restart smoke before using a runtime root for expensive work.
- If re-attach fails because the tracked pid is gone or the command no longer matches, the controller moves that entry to `jobs/failed/` with a bootstrap reconciliation reason instead of silently ignoring it.

## Lease

- One runtime root should have exactly one active controller.
- Ownership is guarded by:
  - `state/controller.lock/`
  - `state/controller_lease.json`
- If a second controller points at the same runtime root while the lease is fresh, it should exit immediately.
- If the lease is stale, the next controller can reclaim it and continue.

## Stable Discovery

- Do not hard-code the newest `platform_<task_id>` path in downstream automation.
- Prefer:
  - `runtime/current_runtime.json`
  - `runtime/state/controller_state.json`
  - `runtime/state/controller_lease.json`
- `current_runtime.json` is the controller-owned pointer for the active runtime.
- The mirrored `runtime/state/*.json` files let callers resolve the current runtime from the sandbox root.

## Human-Readable Status

- Use `submit_job.py --status-summary` when you want a quick operator view instead of raw JSON.
- It can resolve from either:
  - `--root runtime/platform_<task_id>`
  - `--root <sandbox>`
- GPUs with utilization but process rows that lack `job=` are the main clue that some other process is occupying the node outside the queue.
- For a richer operator UI, run `dashboard_server.py` against the sandbox root on `dev-intern-01/02`, preferably under `tmux`.

## Auditability

- Prefer `submit_control.py` or `submit_job.py --kill` over any direct file writes.
- `submit_job.py --kill` now writes an audited control request instead of dropping an anonymous `jobs/kill/<job_id>.json`.
- Key audit artifacts:
  - `logs/controller_events.jsonl`
  - `logs/controller_admin_audit.jsonl`
  - `logs/jobs/<job_id>.events.jsonl`
- The legacy `jobs/kill/` compatibility path is disabled by default. Re-enable it only if you really need backward compatibility and accept the attribution gap.

## Dev Machine Background Services

- Do not rely on the PC for long-running operator services.
- Preferred pattern on `dev-intern-01/02`:
  - dashboard in tmux:
    - `bash scripts/run_dashboard_tmux.sh <sandbox-root> 8787`
  - host-side controller supervisor in tmux:
    - `bash scripts/run_controller_supervisor_tmux.sh <sandbox-root> <submit-config>`
- Both wrappers write logs back into vePFS under:
  - `<sandbox-root>/runtime/host_services/`
- If the dev machine restarts, those host-side services stop, but the already-running Volc controller task and the vePFS logs remain the main source of truth.

## No Platform Stop Permission Workaround

If the current account cannot call `volc ml_task cancel`, do not assume the controller is uncontrollable.

Use the runtime control queue instead:

1. `submit_control.py --action cancel_active_job`
2. `submit_control.py --action purge_queue`
3. `submit_control.py --action retire_controller --cancel-active-job --purge-queue`

This only works for controllers started from the newer controller code that watches `control/queue/`.

## Timeout

The examples here use `ActiveDeadlineSeconds=1296000` (`15` days).

If you intentionally want shorter occupancy, lower it in the submit YAML, but do so knowingly. The historical 12-hour value caused a platform-side stop during a live run.
