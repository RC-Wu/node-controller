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

## Timeout

The examples here use `ActiveDeadlineSeconds=432000`.

If you intentionally want shorter occupancy, lower it in the submit YAML, but do so knowingly. The historical 12-hour value caused a platform-side stop during a live run.
