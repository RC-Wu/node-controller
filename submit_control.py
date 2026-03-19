#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Write a control request into the controller control queue.")
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--action", required=True, choices=["cancel_active_job", "purge_queue", "stop_controller", "drain_and_stop", "retire_controller"])
    ap.add_argument("--request-id", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--signal", default="TERM")
    ap.add_argument("--grace-seconds", type=float, default=15.0)
    ap.add_argument("--job-id", action="append", default=[], help="Optional queued job id selector for purge_queue.")
    ap.add_argument("--purge-queue", action="store_true")
    ap.add_argument("--cancel-active-job", action="store_true")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    request_id = args.request_id or f"{args.action}_{time.strftime('%Y%m%dT%H%M%S')}"
    control_queue = args.root.resolve() / "control" / "queue"
    control_queue.mkdir(parents=True, exist_ok=True)
    payload = {
        "request_id": request_id,
        "action": args.action,
        "reason": args.reason or args.action,
        "signal": args.signal,
        "grace_seconds": args.grace_seconds,
        "job_ids": args.job_id,
        "purge_queue": args.purge_queue,
        "cancel_active_job": args.cancel_active_job,
        "submitted_at_epoch": time.time(),
    }
    out = control_queue / f"{request_id}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
