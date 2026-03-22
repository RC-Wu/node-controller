from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def try_load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = load_json(path)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def resolve_runtime_root(root: Path, *, allow_candidate_fallback: bool = False) -> Path:
    candidate = root.resolve()
    if (candidate / "jobs").is_dir() and (candidate / "state").is_dir():
        return candidate

    pointer_candidates = [
        candidate / "current_runtime.json",
        candidate / "runtime" / "current_runtime.json",
    ]
    for pointer in pointer_candidates:
        payload = try_load_json(pointer)
        if payload is None:
            continue
        runtime_root = str(payload.get("runtime_root") or "").strip()
        if runtime_root:
            return Path(runtime_root).resolve()

    mirror_candidates = [
        candidate / "state" / "controller_state.json",
        candidate / "runtime" / "state" / "controller_state.json",
    ]
    for state_path in mirror_candidates:
        payload = try_load_json(state_path)
        if payload is None:
            continue
        runtime_root = str(payload.get("runtime_root") or "").strip()
        if runtime_root:
            return Path(runtime_root).resolve()

    if allow_candidate_fallback:
        return candidate

    raise RuntimeError(
        f"Could not resolve a controller runtime from {candidate}. "
        "Pass a runtime root or a sandbox/runtime root containing current_runtime.json."
    )


def load_controller_state(root: Path) -> dict[str, Any]:
    runtime_root = resolve_runtime_root(root)
    state_path = runtime_root / "state" / "controller_state.json"
    payload = try_load_json(state_path)
    if payload is None:
        raise RuntimeError(f"missing or invalid controller state: {state_path}")
    payload.setdefault("runtime_root", str(runtime_root))
    return payload


def load_controller_lease(root: Path) -> dict[str, Any]:
    runtime_root = resolve_runtime_root(root)
    lease_path = runtime_root / "state" / "controller_lease.json"
    payload = try_load_json(lease_path)
    if payload is None:
        raise RuntimeError(f"missing or invalid controller lease: {lease_path}")
    payload.setdefault("runtime_root", str(runtime_root))
    return payload


def count_json_files(path: Path, *, include_meta: bool = False, include_results: bool = False) -> int:
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


def runtime_counts(runtime_root: Path) -> dict[str, int]:
    return {
        "queue": count_json_files(runtime_root / "jobs" / "queue"),
        "running_specs": count_json_files(runtime_root / "jobs" / "running"),
        "running_meta": count_json_files(runtime_root / "jobs" / "running", include_meta=True)
        - count_json_files(runtime_root / "jobs" / "running"),
        "done": count_json_files(runtime_root / "jobs" / "done", include_results=True),
        "failed": count_json_files(runtime_root / "jobs" / "failed", include_results=True),
        "cancelled": count_json_files(runtime_root / "jobs" / "cancelled", include_results=True),
        "kill": count_json_files(runtime_root / "jobs" / "kill"),
        "control_queue": count_json_files(runtime_root / "control" / "queue"),
        "control_done": count_json_files(runtime_root / "control" / "done", include_results=True),
        "control_failed": count_json_files(runtime_root / "control" / "failed", include_results=True),
    }


def tail_text(path: Path, line_count: int) -> list[str]:
    if line_count <= 0 or not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return list(deque((line.rstrip("\n") for line in handle), maxlen=line_count))


def tail_jsonl(path: Path, line_count: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in tail_text(path, line_count):
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def list_job_rows(runtime_root: Path, *, limit_per_bucket: int = 200) -> list[dict[str, Any]]:
    job_rows: list[dict[str, Any]] = []
    buckets = [
        ("queue", runtime_root / "jobs" / "queue"),
        ("running", runtime_root / "jobs" / "running"),
        ("done", runtime_root / "jobs" / "done"),
        ("failed", runtime_root / "jobs" / "failed"),
        ("cancelled", runtime_root / "jobs" / "cancelled"),
    ]
    for bucket_name, bucket_path in buckets:
        if not bucket_path.exists():
            continue
        items = sorted(bucket_path.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True)
        seen = 0
        for item in items:
            if not item.is_file() or item.suffix != ".json":
                continue
            if bucket_name == "running" and item.name.endswith(".meta.json"):
                continue
            payload = try_load_json(item) or {}
            job_rows.append(
                {
                    "bucket": bucket_name,
                    "name": item.name,
                    "path": str(item),
                    "mtime_epoch": item.stat().st_mtime,
                    "job_id": str(payload.get("job_id") or item.stem.replace(".result", "")),
                    "status": payload.get("status"),
                    "payload": payload,
                }
            )
            seen += 1
            if seen >= limit_per_bucket:
                break
    return sorted(job_rows, key=lambda item: item["mtime_epoch"], reverse=True)


def build_runtime_bundle(root: Path) -> dict[str, Any]:
    runtime_root = resolve_runtime_root(root)
    state_path = runtime_root / "state" / "controller_state.json"
    lease_path = runtime_root / "state" / "controller_lease.json"
    pointer_path = runtime_root.parent / "current_runtime.json"
    return {
        "runtime_root": str(runtime_root),
        "controller_state_path": str(state_path),
        "controller_lease_path": str(lease_path),
        "pointer_path": str(pointer_path),
        "controller_state": try_load_json(state_path) or {},
        "controller_lease": try_load_json(lease_path) or {},
        "runtime_pointer": try_load_json(pointer_path) or {},
        "counts": runtime_counts(runtime_root),
    }
