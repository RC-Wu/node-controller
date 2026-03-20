#!/usr/bin/env python3
"""
Submit a job to the node controller queue, or kill a running job.

Usage:
  # Submit a training job on 4 GPUs
  python submit_job.py --runtime-root /dev_vepfs/xiyu/node-controller/runtime/platform_TASK_ID \
    --job-id train_v1 --gpus 4 --workdir /dev_vepfs/xiyu/armesh-dev/work/vae-v1 \
    --command bash scripts/train_dense256.sh

  # Kill a running job
  python submit_job.py --runtime-root ... --kill train_v1

  # Check controller status
  python submit_job.py --runtime-root ... --status
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime-root", type=Path, required=True)
    ap.add_argument("--job-id", type=str)
    ap.add_argument("--gpus", type=int, default=4)
    ap.add_argument("--command", nargs="+")
    ap.add_argument("--workdir", type=str)
    ap.add_argument("--env", type=str, action="append", default=[], help="KEY=VALUE")
    ap.add_argument("--timeout", type=int, default=432000, help="Job timeout in seconds")
    ap.add_argument("--kill", type=str, metavar="JOB_ID", help="Kill a running job")
    ap.add_argument("--status", action="store_true", help="Show controller status")
    args = ap.parse_args()

    root = args.runtime_root

    if args.status:
        state_path = root / "state" / "controller_state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
            print(json.dumps(state, indent=2))
        else:
            print("No controller state found.", file=sys.stderr)
            return 1
        return 0

    if args.kill:
        kill_dir = root / "jobs" / "kill"
        kill_dir.mkdir(parents=True, exist_ok=True)
        signal_file = kill_dir / args.kill
        signal_file.write_text(f'{{"kill_requested_at": {__import__("time").time()}}}\n')
        print(f"Kill signal sent for job: {args.kill}")
        return 0

    if not args.job_id or not args.command:
        ap.error("--job-id and --command are required for job submission")

    env = {}
    for kv in args.env:
        k, v = kv.split("=", 1)
        env[k] = v

    spec = {
        "job_id": args.job_id,
        "command": args.command,
        "gpus": args.gpus,
        "workdir": args.workdir or str(root),
        "env": env,
        "timeout_seconds": args.timeout,
    }

    queue_dir = root / "jobs" / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    spec_path = queue_dir / f"{args.job_id}.json"
    spec_path.write_text(json.dumps(spec, indent=2) + "\n")
    print(f"Submitted job {args.job_id} ({args.gpus} GPUs) → {spec_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
