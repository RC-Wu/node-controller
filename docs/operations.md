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

## No Platform Stop Permission Workaround

If the current account cannot call `volc ml_task cancel`, do not assume the controller is uncontrollable.

Use the runtime control queue instead:

1. `submit_control.py --action cancel_active_job`
2. `submit_control.py --action purge_queue`
3. `submit_control.py --action retire_controller --cancel-active-job --purge-queue`

This only works for controllers started from the newer controller code that watches `control/queue/`.

## Timeout

The examples here use `ActiveDeadlineSeconds=432000`.

If you intentionally want shorter occupancy, lower it in the submit YAML, but do so knowingly. The historical 12-hour value caused a platform-side stop during a live run.
