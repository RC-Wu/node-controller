#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path

from runtime_io import resolve_runtime_root, write_json_atomic


SCHEMA_VERSION = 2


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
    ap.add_argument("--requested-by", default="", help="Optional human/automation label for audit trails.")
    ap.add_argument("--request-source", default="cli", help="Source label recorded in audit trails.")
    ap.add_argument(
        "--replace-if-exists",
        action="store_true",
        help="Allow replacing an existing control request file with the same request id.",
    )
    return ap.parse_args()

def request_metadata(args: argparse.Namespace) -> dict:
    return {
        "requested_by": args.requested_by or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
        "requested_from_host": socket.gethostname(),
        "requested_from_pid": os.getpid(),
        "requested_from_argv": sys.argv,
        "request_source": args.request_source,
    }


def main() -> int:
    args = parse_args()
    request_id = args.request_id or f"{args.action}_{time.strftime('%Y%m%dT%H%M%S')}"
    control_queue = resolve_runtime_root(args.root, allow_candidate_fallback=True) / "control" / "queue"
    control_queue.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "action": args.action,
        "reason": args.reason or args.action,
        "signal": args.signal,
        "grace_seconds": args.grace_seconds,
        "job_ids": args.job_id,
        "purge_queue": args.purge_queue,
        "cancel_active_job": args.cancel_active_job,
        "submitted_at_epoch": time.time(),
        **request_metadata(args),
    }
    out = control_queue / f"{request_id}.json"
    if out.exists() and not args.replace_if_exists:
        raise SystemExit(f"{out} already exists; pass --replace-if-exists to overwrite")
    write_json_atomic(out, payload)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
