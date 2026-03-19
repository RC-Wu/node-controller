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
        "submitted_at_epoch": time.time(),
    }
    out = queue_dir / f"{args.job_id}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
