#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

from runtime_io import load_controller_state, resolve_runtime_root, write_json_atomic


SCHEMA_VERSION = 2


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Write a job spec into the prototype dispatcher queue.")
    ap.add_argument("--root", type=Path)
    ap.add_argument("--runtime-root", type=Path, dest="runtime_root")
    ap.add_argument("--job-id", default="")
    ap.add_argument("--workdir", default="")
    ap.add_argument("--timeout-seconds", type=float, default=0)
    ap.add_argument("--gpu-indices", default="", help="Explicit GPU slice, e.g. 0,1")
    ap.add_argument("--gpu-count", type=int, default=0, help="Number of GPUs to allocate if gpu-indices is not set.")
    ap.add_argument("--allowed-gpu-indices", default="", help="Eligible GPU pool for first-fit allocation, e.g. 2,3,4,5")
    ap.add_argument("--priority", type=int, default=0, help="Higher values launch first when multiple queue items fit.")
    ap.add_argument("--exclusive", action="store_true", help="Require no other jobs to be active before launch.")
    ap.add_argument(
        "--replace-if-exists",
        action="store_true",
        help="Allow replacing an existing queued spec with the same job id.",
    )
    ap.add_argument(
        "--set-cuda-visible-devices",
        action="store_true",
        help="Export CUDA_VISIBLE_DEVICES from the allocated slice when the controller chooses GPUs from schema fields.",
    )
    ap.add_argument("--kill", default="", help="Request cancellation via the audited control queue.")
    ap.add_argument("--status", action="store_true", help="Print state/controller_state.json and exit.")
    ap.add_argument("--status-summary", action="store_true", help="Print a human-readable live summary instead of raw JSON.")
    ap.add_argument(
        "--status-watch-seconds",
        type=float,
        default=0.0,
        help="Refresh --status-summary every N seconds, similar to watch nvidia-smi.",
    )
    ap.add_argument("--requested-by", default="", help="Optional human/automation label for audit trails.")
    ap.add_argument("--request-source", default="cli", help="Source label recorded in audit trails.")
    ap.add_argument("--env", action="append", default=[], help="KEY=VALUE")
    ap.add_argument("command", nargs=argparse.REMAINDER)
    return ap.parse_args()


def format_age_seconds(updated_at_epoch: float | int | None) -> str:
    try:
        updated = float(updated_at_epoch or 0.0)
    except Exception:
        return "unknown"
    if updated <= 0:
        return "unknown"
    age = max(0.0, time.time() - updated)
    return f"{age:.1f}s"


def render_status_summary(state: dict) -> str:
    counts = dict(state.get("counts") or {})
    queue_count = int(state.get("queue_count", counts.get("queue", 0)) or 0)
    done_count = int(state.get("done_count", counts.get("done", 0)) or 0)
    failed_count = int(state.get("failed_count", counts.get("failed", 0)) or 0)
    cancelled_count = int(state.get("cancelled_count", counts.get("cancelled", 0)) or 0)
    control_queue_count = int(state.get("control_queue_count", counts.get("control_queue", 0)) or 0)
    heartbeat_age = format_age_seconds(state.get("updated_at_epoch"))
    runtime_root = state.get("runtime_root", "")
    managed_gpus = ",".join(str(item) for item in state.get("managed_gpu_indices") or [])
    active_jobs = state.get("active_jobs") or []

    process_map: dict[int, list[str]] = {}
    for proc in state.get("gpu_processes") or []:
        try:
            gpu_index = int(proc.get("gpu_index"))
        except Exception:
            continue
        label = f"pid={proc.get('pid')} {proc.get('process_name', '?')} mem={proc.get('used_memory_mb', '?')}MB"
        if proc.get("controller_job_id"):
            label += f" job={proc['controller_job_id']}"
        process_map.setdefault(gpu_index, []).append(label)

    lines = [
        f"runtime_root: {runtime_root}",
        f"controller: host={state.get('hostname', '?')} pid={state.get('pid', '?')} heartbeat_age={heartbeat_age} managed_gpus={managed_gpus or '-'}",
        (
            "jobs: "
            f"active={int(state.get('active_job_count', 0) or 0)} "
            f"queue={queue_count} done={done_count} failed={failed_count} cancelled={cancelled_count} "
            f"control_queue={control_queue_count}"
        ),
    ]

    if active_jobs:
        lines.append("active_jobs:")
        for job in active_jobs:
            command = " ".join(str(part) for part in job.get("command") or [])
            lines.append(
                "  "
                + f"{job.get('job_id', '?')} "
                + f"gpu={','.join(str(x) for x in job.get('allocated_gpu_indices') or []) or '-'} "
                + f"pid={job.get('pid', '?')} "
                + f"workdir={job.get('workdir', '')}"
            )
            if command:
                lines.append("  " + f"cmd={command}")
    else:
        lines.append("active_jobs: none")

    gpu_rows = state.get("gpu_status") or []
    if gpu_rows:
        lines.append("gpu_status:")
        for row in gpu_rows:
            idx = int(row.get("index", -1))
            proc_text = "; ".join(process_map.get(idx, [])) or "-"
            lines.append(
                "  "
                + f"gpu{idx}: util={row.get('util_pct', '?')}% "
                + f"mem={row.get('mem_used_mb', '?')}/{row.get('mem_total_mb', '?')}MB "
                + f"temp={row.get('temp_c', '?')}C "
                + f"procs={proc_text}"
            )
    else:
        lines.append("gpu_status: unavailable")
    return "\n".join(lines)


def clear_screen() -> None:
    if os.name == "nt":
        os.system("cls")
        return
    print("\033[2J\033[H", end="")


def maybe_print_status(root: Path, *, raw: bool, summary: bool, watch_seconds: float) -> int:
    if not raw and not summary and watch_seconds <= 0:
        return -1
    if watch_seconds > 0 and not summary:
        summary = True

    while True:
        state = load_controller_state(root)
        if summary:
            if watch_seconds > 0:
                clear_screen()
            print(render_status_summary(state))
        else:
            print(json.dumps(state, ensure_ascii=False, indent=2))
        if watch_seconds <= 0:
            return 0
        time.sleep(max(0.2, watch_seconds))


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
    root_arg = args.root or args.runtime_root
    if root_arg is None:
        raise SystemExit("--root or --runtime-root is required")

    status_rc = maybe_print_status(
        root_arg,
        raw=args.status,
        summary=args.status_summary,
        watch_seconds=args.status_watch_seconds,
    )
    if status_rc >= 0:
        return status_rc

    root = resolve_runtime_root(root_arg, allow_candidate_fallback=True)

    if args.kill:
        control_queue = root / "control" / "queue"
        request_id = f"cancel_active_job_{args.kill}_{time.strftime('%Y%m%dT%H%M%S')}"
        out = control_queue / f"{request_id}.json"
        payload = {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "action": "cancel_active_job",
            "reason": f"submit_job --kill {args.kill}",
            "signal": "TERM",
            "grace_seconds": 15.0,
            "job_ids": [args.kill],
            "purge_queue": False,
            "cancel_active_job": False,
            "submitted_at_epoch": time.time(),
            **request_metadata(args),
        }
        if out.exists() and not args.replace_if_exists:
            raise SystemExit(f"{out} already exists; pass --replace-if-exists to overwrite")
        write_json_atomic(out, payload)
        print(out)
        return 0

    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.job_id:
        raise SystemExit("--job-id is required for job submission")
    if not args.command:
        raise SystemExit("command is required after --")
    env = {}
    for item in args.env:
        key, value = item.split("=", 1)
        env[key] = value
    queue_dir = root / "jobs" / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "job_id": args.job_id,
        "command": args.command,
        "workdir": args.workdir,
        "env": env,
        "timeout_seconds": args.timeout_seconds,
        "priority": args.priority,
        "exclusive": args.exclusive,
        "set_cuda_visible_devices": args.set_cuda_visible_devices,
        "submitted_at_epoch": time.time(),
        **request_metadata(args),
    }
    if args.gpu_indices:
        payload["gpu_indices"] = [item.strip() for item in args.gpu_indices.split(",") if item.strip()]
    if args.gpu_count > 0:
        payload["gpu_count"] = args.gpu_count
    if args.allowed_gpu_indices:
        payload["allowed_gpu_indices"] = [item.strip() for item in args.allowed_gpu_indices.split(",") if item.strip()]
    out = queue_dir / f"{args.job_id}.json"
    if out.exists() and not args.replace_if_exists:
        raise SystemExit(f"{out} already exists; pass --replace-if-exists to overwrite")
    write_json_atomic(out, payload)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
