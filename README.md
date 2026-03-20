# node-controller

Queue-driven GPU job scheduler for long-lived Volc ML Platform custom tasks.

## v3 — Fork-Based Architecture (slurm-style)

```
controller.py (immortal daemon — never touches job code)
  └── fork → job_supervisor.py (per-job, isolated process)
                └── exec → training command (with CUDA_VISIBLE_DEVICES set)
```

The controller runs inside one long-lived ML Platform task and watches a shared runtime root on vePFS.
It forks a `job_supervisor.py` for each job — if the training crashes or the supervisor segfaults,
only that job dies. The controller continues running and releases the GPUs.

### Features

- **Multi-GPU parallelism**: Run multiple jobs concurrently with automatic GPU pool allocation (contiguous-first for NVLink)
- **Job-level kill**: Drop a file in `jobs/kill/<job_id>` to kill a specific job without cancelling the whole Volc task
- **Fork-based isolation**: Controller never opens job log files, never runs job code — zero exposure to job failures
- **Process group kill**: `os.setsid` + `os.killpg` ensures DDP child workers are cleaned up
- **Line-buffered stdout**: `stdbuf -oL` wrapper for near-real-time log tailing over vePFS
- **GPU status monitoring**: `nvidia-smi` probe in every heartbeat (util%, memory, temperature)
- **VEPFS quota handling**: Detects disk-full, pauses with retry loop, auto-resumes when space freed
- **Graceful SIGTERM**: Kills all active supervisors before controller exits, writes final shutdown state
- **Structured logging**: Timestamped logs to `logs/controller.log` (file + stderr)
- **Exception resilience**: Unhandled exceptions in main loop are logged and recovered with backoff

### Runtime Layout

```
runtime/platform_<TASK_ID>/
├── jobs/
│   ├── queue/          # Drop JSON specs here to submit jobs
│   ├── running/        # Controller moves specs here while running
│   ├── done/           # Completed jobs (exit 0)
│   ├── failed/         # Failed/killed jobs
│   └── kill/           # Drop any file named <job_id> to kill that job
├── logs/
│   ├── controller.log              # Controller structured log
│   ├── controller_heartbeat.jsonl  # Event timeline
│   └── jobs/<job_id>.log           # Per-job stdout/stderr
└── state/
    └── controller_state.json       # Heartbeat: active jobs, GPU pool, GPU status
```

### Job Spec Format

```json
{
  "job_id": "train_v1",
  "command": ["bash", "scripts/train.sh"],
  "gpus": 4,
  "workdir": "/path/to/workdir",
  "env": {"HYDRA_FULL_ERROR": "1"},
  "timeout_seconds": 432000
}
```

- `gpus`: Number of GPUs needed (default: all). Controller auto-assigns indices via `CUDA_VISIBLE_DEVICES`.
- `timeout_seconds`: Kill job after this many seconds (0 = no timeout).

### Usage

```bash
# Submit a job
python submit_job.py --runtime-root $RUNTIME \
  --job-id train_v1 --gpus 4 \
  --command bash scripts/train.sh \
  --workdir /path/to/code

# Kill a running job
python submit_job.py --runtime-root $RUNTIME --kill train_v1

# Check controller status
python submit_job.py --runtime-root $RUNTIME --status

# Or just read the state file
cat $RUNTIME/state/controller_state.json
```

### Controller State Example

```json
{
  "updated_at_epoch": 1774001696.69,
  "pid": 2124,
  "status": "running",
  "total_gpus": 8,
  "free_gpus": [4, 5, 6, 7],
  "active_jobs": [
    {
      "job_id": "train_v1",
      "gpus": [0, 1, 2, 3],
      "supervisor_pid": 2480,
      "started_at_epoch": 1774001700.0,
      "elapsed_seconds": 3600.0
    }
  ],
  "gpu_status": [
    {"index": 0, "util_pct": 98, "mem_used_mb": 74000, "mem_total_mb": 81920, "temp_c": 65},
    ...
  ]
}
```

## Repo Layout

- `controller.py`: Main daemon — polls queue, manages GPU pool, forks supervisors, writes heartbeat
- `job_supervisor.py`: Per-job process — sets up env, launches training, monitors timeout, forwards SIGTERM
- `submit_job.py`: CLI helper for submitting, killing, and checking jobs
- `volc_dispatcher_entry.sh`: Volc task entrypoint (creates runtime dirs, launches controller)
- `examples/`: Minimal Volc task submit configs

## Deployment

1. Place all files on shared storage (vePFS)
2. Set `Entrypoint` in your Volc YAML to `bash /path/to/volc_dispatcher_entry.sh`
3. Submit: `volc ml_task submit -c your_task.yaml`
4. Queue jobs by writing JSON to `runtime/.../jobs/queue/`
