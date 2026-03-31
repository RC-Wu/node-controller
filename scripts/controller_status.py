#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Render a lightweight human-readable status view for the controller runtime.")
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--root", type=Path, help="Explicit runtime root, e.g. runtime/platform_<task_id>.")
    target.add_argument(
        "--sandbox-root",
        type=Path,
        help="Controller sandbox root. The script will resolve runtime/current_runtime.json under this sandbox.",
    )
    ap.add_argument("--json", action="store_true", help="Print the resolved payloads as JSON instead of a table view.")
    return ap.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def try_read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = read_json(path)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def format_age(epoch: float | int | None) -> str:
    if epoch is None:
        return "-"
    try:
        delta = max(0.0, time.time() - float(epoch))
    except Exception:
        return "-"
    if delta < 1.0:
        return f"{delta * 1000:.0f}ms"
    if delta < 60.0:
        return f"{delta:.1f}s"
    if delta < 3600.0:
        return f"{delta / 60.0:.1f}m"
    return f"{delta / 3600.0:.1f}h"


def safe_count_json(path: Path, *, include_meta: bool = False, include_results: bool = False) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.iterdir():
        if not item.is_file() or item.suffix != ".json":
            continue
        if not include_meta and item.name.endswith(".meta.json"):
            continue
        if not include_results and item.name.endswith(".result.json"):
            continue
        total += 1
    return total


def resolve_runtime_root(args: argparse.Namespace) -> tuple[Path, dict[str, Any] | None, Path | None]:
    if args.root is not None:
        runtime_root = args.root.resolve()
        return runtime_root, None, None

    assert args.sandbox_root is not None
    sandbox_root = args.sandbox_root.resolve()
    pointer_path = sandbox_root / "runtime" / "current_runtime.json"
    pointer = try_read_json(pointer_path)
    if pointer is None:
        raise SystemExit(f"missing or invalid runtime pointer: {pointer_path}")
    runtime_root_text = str(pointer.get("runtime_root") or "").strip()
    if not runtime_root_text:
        raise SystemExit(f"{pointer_path} does not contain runtime_root")
    return Path(runtime_root_text), pointer, pointer_path


def load_status_bundle(runtime_root: Path) -> dict[str, Any]:
    state_path = runtime_root / "state" / "controller_state.json"
    lease_path = runtime_root / "state" / "controller_lease.json"
    bundle = {
        "runtime_root": str(runtime_root),
        "controller_state_path": str(state_path),
        "controller_lease_path": str(lease_path),
        "controller_state": try_read_json(state_path) or {},
        "controller_lease": try_read_json(lease_path) or {},
        "counts": {
            "queue": safe_count_json(runtime_root / "jobs" / "queue"),
            "running_specs": safe_count_json(runtime_root / "jobs" / "running"),
            "running_meta": safe_count_json(runtime_root / "jobs" / "running", include_meta=True) - safe_count_json(runtime_root / "jobs" / "running"),
            "done": safe_count_json(runtime_root / "jobs" / "done", include_results=True),
            "failed": safe_count_json(runtime_root / "jobs" / "failed", include_results=True),
            "cancelled": safe_count_json(runtime_root / "jobs" / "cancelled", include_results=True),
            "kill": safe_count_json(runtime_root / "jobs" / "kill"),
            "control_queue": safe_count_json(runtime_root / "control" / "queue"),
            "control_done": safe_count_json(runtime_root / "control" / "done", include_results=True),
            "control_failed": safe_count_json(runtime_root / "control" / "failed", include_results=True),
        },
    }
    return bundle


def gpu_owner_by_index(controller_state: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for job in controller_state.get("active_jobs") or []:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("job_id") or "?")
        for index in job.get("allocated_gpu_indices") or []:
            mapping[str(index)] = job_id
    return mapping


def gpu_process_summary(controller_state: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    summary: dict[str, list[dict[str, Any]]] = {}
    for proc in controller_state.get("gpu_processes") or []:
        if not isinstance(proc, dict):
            continue
        key = str(proc.get("gpu_index"))
        summary.setdefault(key, []).append(proc)
    return summary


def render_text(bundle: dict[str, Any], *, pointer: dict[str, Any] | None, pointer_path: Path | None) -> str:
    state = bundle["controller_state"]
    lease = bundle["controller_lease"]
    counts = bundle["counts"]
    owner_map = gpu_owner_by_index(state)
    proc_map = gpu_process_summary(state)

    lines: list[str] = []
    lines.append("Controller Status")
    lines.append(f"runtime_root: {bundle['runtime_root']}")
    if pointer_path is not None:
        lines.append(f"runtime_pointer: {pointer_path}")
    if pointer:
        lines.append(f"pointer_updated: {format_age(pointer.get('updated_at_epoch'))} ago")
    lines.append(
        "controller: "
        f"pid={state.get('pid', '-')} host={state.get('hostname', '-')} "
        f"heartbeat_age={format_age(state.get('updated_at_epoch'))} lease_age={format_age(lease.get('updated_at_epoch'))}"
    )
    lines.append(
        "counts: "
        f"queue={state.get('queue_count', counts['queue'])} "
        f"running={state.get('running_count', counts['running_specs'])} "
        f"done={state.get('done_count', counts['done'])} "
        f"failed={state.get('failed_count', counts['failed'])} "
        f"cancelled={state.get('cancelled_count', counts['cancelled'])} "
        f"kill={state.get('kill_count', counts['kill'])} "
        f"control_queue={state.get('control_queue_count', counts['control_queue'])}"
    )
    lines.append("")
    lines.append("GPUs")
    lines.append("idx util mem(MB) temp owner processes note")
    active_gpu = {str(item) for item in state.get("active_gpu_indices") or []}
    external_blocked = {str(item) for item in state.get("externally_blocked_gpu_indices") or []}
    active_untracked = {str(item) for item in state.get("active_untracked_gpu_indices") or []}
    block_reasons = state.get("gpu_external_block_reasons") or {}
    for gpu in state.get("gpu_status") or []:
        if not isinstance(gpu, dict):
            continue
        index = str(gpu.get("index", "?"))
        owner = owner_map.get(index, "-")
        proc_rows = proc_map.get(index, [])
        note_parts: list[str] = []
        if index in active_gpu:
            note_parts.append(f"controller_job:{owner}" if owner != "-" else "controller_active")
        elif owner == "-" and proc_rows:
            note_parts.append("external_or_untracked")
        if index in external_blocked:
            note_parts.append("external_blocked")
        if index in active_untracked:
            note_parts.append("active_untracked_proc")
        if block_reasons.get(index):
            note_parts.extend(str(item) for item in block_reasons[index])
        note = " | ".join(note_parts) if note_parts else "-"
        lines.append(
            f"{index:>3} "
            f"{str(gpu.get('util_pct', '-')):>4} "
            f"{str(gpu.get('mem_used_mb', '-')):>7}/{str(gpu.get('mem_total_mb', '-')):<7} "
            f"{str(gpu.get('temp_c', '-')):>4} "
            f"{owner:<24} "
            f"{len(proc_rows):>3} "
            f"{note}"
        )
    if not (state.get("gpu_status") or []):
        lines.append("(no gpu_status rows)")
    lines.append("")
    lines.append("Active Jobs")
    active_jobs = state.get("active_jobs") or []
    if not active_jobs:
        lines.append("(none)")
    else:
        lines.append("job_id gpus pid timeout_seconds workdir")
        for job in active_jobs:
            if not isinstance(job, dict):
                continue
            lines.append(
                f"{str(job.get('job_id', '-')):<24} "
                f"{','.join(str(x) for x in (job.get('allocated_gpu_indices') or [])) or '-':<12} "
                f"{str(job.get('pid', '-')):<8} "
                f"{str(job.get('timeout_seconds', '-')):<15} "
                f"{str(job.get('workdir', '-'))}"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    runtime_root, pointer, pointer_path = resolve_runtime_root(args)
    bundle = load_status_bundle(runtime_root)
    if args.json:
        print(
            json.dumps(
                {
                    "pointer_path": None if pointer_path is None else str(pointer_path),
                    "pointer": pointer,
                    **bundle,
                },
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    sys_text = render_text(bundle, pointer=pointer, pointer_path=pointer_path)
    print(sys_text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
