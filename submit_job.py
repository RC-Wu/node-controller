#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Write a job spec into the prototype dispatcher queue.")
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--workdir", default="")
    ap.add_argument("--timeout-seconds", type=float, default=0)
    ap.add_argument("--gpu-indices", default="", help="Explicit GPU slice, e.g. 0,1")
    ap.add_argument("--gpu-count", type=int, default=0, help="Number of GPUs to allocate if gpu-indices is not set.")
    ap.add_argument("--allowed-gpu-indices", default="", help="Eligible GPU pool for first-fit allocation, e.g. 2,3,4,5")
    ap.add_argument("--priority", type=int, default=0, help="Higher values launch first when multiple queue items fit.")
    ap.add_argument("--exclusive", action="store_true", help="Require no other jobs to be active before launch.")
    ap.add_argument(
        "--set-cuda-visible-devices",
        action="store_true",
        help="Export CUDA_VISIBLE_DEVICES from the allocated slice when the controller chooses GPUs from schema fields.",
    )
    ap.add_argument("--env", action="append", default=[], help="KEY=VALUE")
    ap.add_argument("command", nargs=argparse.REMAINDER)
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        raise SystemExit("command is required after --")
    env = {}
    for item in args.env:
        key, value = item.split("=", 1)
        env[key] = value
    queue_dir = args.root.resolve() / "jobs" / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_id": args.job_id,
        "command": args.command,
        "workdir": args.workdir,
        "env": env,
        "timeout_seconds": args.timeout_seconds,
        "priority": args.priority,
        "exclusive": args.exclusive,
        "set_cuda_visible_devices": args.set_cuda_visible_devices,
        "submitted_at_epoch": time.time(),
    }
    if args.gpu_indices:
        payload["gpu_indices"] = [item.strip() for item in args.gpu_indices.split(",") if item.strip()]
    if args.gpu_count > 0:
        payload["gpu_count"] = args.gpu_count
    if args.allowed_gpu_indices:
        payload["allowed_gpu_indices"] = [item.strip() for item in args.allowed_gpu_indices.split(",") if item.strip()]
    out = queue_dir / f"{args.job_id}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
