#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Prototype long-lived dispatcher for a Volc custom task.")
    ap.add_argument("--root", type=Path, required=True, help="Runtime root on vePFS/shared storage.")
    ap.add_argument("--poll-seconds", type=float, default=2.0)
    ap.add_argument("--heartbeat-seconds", type=float, default=10.0)
    ap.add_argument(
        "--managed-gpu-indices",
        default="",
        help="Optional comma-separated GPU indices managed by this controller. Defaults to auto-detect via nvidia-smi.",
    )
    ap.add_argument("--once", action="store_true", help="Process at most one visible queue item, then exit.")
    return ap.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def bool_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def normalize_gpu_indices(value: Any) -> list[str]:
    parts: list[str] = []
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        for item in value:
            parts.extend(normalize_gpu_indices(item))
        return parts
    text = str(value).strip()
    if not text:
        return []
    for part in text.split(","):
        normalized = part.strip()
        if not normalized:
            continue
        if not normalized.isdigit():
            raise ValueError(f"invalid gpu index {normalized!r}")
        parts.append(str(int(normalized)))
    return sorted(set(parts), key=int)


def signal_from_name(name: str) -> signal.Signals:
    normalized = name.strip().upper()
    mapping = {
        "INT": signal.SIGINT,
        "KILL": signal.SIGKILL,
        "QUIT": signal.SIGQUIT,
        "TERM": signal.SIGTERM,
    }
    return mapping.get(normalized, signal.SIGTERM)


def detect_managed_gpu_indices(override: str) -> list[str]:
    if override.strip():
        return normalize_gpu_indices(override)
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    indices = []
    for line in result.stdout.splitlines():
        text = line.strip()
        if not text or not text.isdigit():
            continue
        indices.append(str(int(text)))
    return sorted(set(indices), key=int)


def parse_command_gpu_hints(command: list[str]) -> list[str]:
    text = " ".join(command)
    explicit: list[str] = []
    for match in re.findall(r"CUDA_VISIBLE_DEVICES=([0-9](?:,[0-9])*)", text):
        explicit.extend(normalize_gpu_indices(match))
    for match in re.findall(r"--gpu-indices\s+([0-9](?:,[0-9])*)", text):
        explicit.extend(normalize_gpu_indices(match))
    return sorted(set(explicit), key=int)


@dataclass
class RuntimeLayout:
    root: Path
    queue_dir: Path
    running_dir: Path
    done_dir: Path
    failed_dir: Path
    cancelled_dir: Path
    logs_dir: Path
    jobs_log_dir: Path
    state_dir: Path
    controller_state: Path
    controller_heartbeat: Path
    control_dir: Path
    control_queue_dir: Path
    control_done_dir: Path
    control_failed_dir: Path


@dataclass
class JobRequest:
    job_id: str
    command: list[str]
    workdir: str
    timeout_seconds: float
    env: dict[str, str]
    requested_gpu_indices: list[str]
    allowed_gpu_indices: list[str]
    gpu_count: int
    exclusive: bool
    set_cuda_visible_devices: bool
    reservation_source: str
    payload: dict[str, Any]


@dataclass
class ActiveRun:
    meta: dict[str, Any]
    proc: subprocess.Popen[str]
    running_spec_path: Path
    running_meta_path: Path
    allocated_gpu_indices: list[str]
    exclusive: bool


def build_layout(root: Path) -> RuntimeLayout:
    jobs_root = root / "jobs"
    logs_root = root / "logs"
    state_root = root / "state"
    control_root = root / "control"
    layout = RuntimeLayout(
        root=root,
        queue_dir=jobs_root / "queue",
        running_dir=jobs_root / "running",
        done_dir=jobs_root / "done",
        failed_dir=jobs_root / "failed",
        cancelled_dir=jobs_root / "cancelled",
        logs_dir=logs_root,
        jobs_log_dir=logs_root / "jobs",
        state_dir=state_root,
        controller_state=state_root / "controller_state.json",
        controller_heartbeat=logs_root / "controller_heartbeat.jsonl",
        control_dir=control_root,
        control_queue_dir=control_root / "queue",
        control_done_dir=control_root / "done",
        control_failed_dir=control_root / "failed",
    )
    for directory in [
        layout.queue_dir,
        layout.running_dir,
        layout.done_dir,
        layout.failed_dir,
        layout.cancelled_dir,
        layout.jobs_log_dir,
        layout.state_dir,
        layout.control_queue_dir,
        layout.control_done_dir,
        layout.control_failed_dir,
    ]:
        directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(layout.root, 0o755)
        os.chmod(jobs_root, 0o755)
        os.chmod(logs_root, 0o755)
        os.chmod(state_root, 0o755)
        os.chmod(control_root, 0o755)
        os.chmod(layout.queue_dir, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO | stat.S_ISVTX)
        os.chmod(layout.control_queue_dir, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO | stat.S_ISVTX)
        os.chmod(layout.running_dir, 0o755)
        os.chmod(layout.done_dir, 0o755)
        os.chmod(layout.failed_dir, 0o755)
        os.chmod(layout.cancelled_dir, 0o755)
        os.chmod(layout.jobs_log_dir, 0o755)
        os.chmod(layout.control_done_dir, 0o755)
        os.chmod(layout.control_failed_dir, 0o755)
    except PermissionError:
        pass
    return layout


def next_visible_json(path: Path) -> Path | None:
    specs = sorted(path.glob("*.json"))
    return specs[0] if specs else None


def next_control_request(layout: RuntimeLayout) -> Path | None:
    return next_visible_json(layout.control_queue_dir)


def load_job_request(spec_path: Path) -> JobRequest:
    payload = read_json(spec_path)
    if "job_id" not in payload:
        raise ValueError(f"{spec_path} missing required field job_id")
    if "command" not in payload:
        raise ValueError(f"{spec_path} missing required field command")
    command = payload["command"]
    if not isinstance(command, list) or not command:
        raise ValueError(f"{spec_path} field command must be a non-empty list")
    env = {str(k): str(v) for k, v in dict(payload.get("env", {})).items()}
    requested_gpu_indices = normalize_gpu_indices(payload.get("gpu_indices"))
    allowed_gpu_indices = normalize_gpu_indices(payload.get("allow_gpu_indices") or payload.get("allowed_gpu_indices"))
    gpu_count = int(payload.get("gpu_count") or 0)
    if requested_gpu_indices:
        reservation_source = "spec.gpu_indices"
    else:
        inferred_env = normalize_gpu_indices(env.get("CUDA_VISIBLE_DEVICES"))
        inferred_cmd = parse_command_gpu_hints([str(part) for part in command])
        if inferred_env:
            requested_gpu_indices = inferred_env
            reservation_source = "env.CUDA_VISIBLE_DEVICES"
        elif inferred_cmd:
            requested_gpu_indices = inferred_cmd
            reservation_source = "command.gpu_hints"
        elif gpu_count > 0:
            reservation_source = "spec.gpu_count"
        else:
            reservation_source = "exclusive-fallback"
    exclusive_default = reservation_source == "exclusive-fallback"
    exclusive = bool_value(payload.get("exclusive"), default=exclusive_default)
    set_cuda_visible_devices = bool_value(
        payload.get("set_cuda_visible_devices"),
        default=reservation_source == "spec.gpu_count",
    )
    return JobRequest(
        job_id=str(payload["job_id"]),
        command=[str(part) for part in command],
        workdir=str(payload.get("workdir", "")),
        timeout_seconds=float(payload.get("timeout_seconds", 0) or 0),
        env=env,
        requested_gpu_indices=requested_gpu_indices,
        allowed_gpu_indices=allowed_gpu_indices,
        gpu_count=gpu_count,
        exclusive=exclusive,
        set_cuda_visible_devices=set_cuda_visible_devices,
        reservation_source=reservation_source,
        payload=payload,
    )


def queued_specs(layout: RuntimeLayout) -> list[Path]:
    def sort_key(path: Path) -> tuple[int, float, str]:
        try:
            payload = read_json(path)
        except Exception:
            return (0, 0.0, path.name)
        priority = int(payload.get("priority") or 0)
        submitted_at = float(payload.get("submitted_at_epoch") or 0.0)
        return (-priority, submitted_at, path.name)

    return sorted(layout.queue_dir.glob("*.json"), key=sort_key)


def active_gpu_indices(active_runs: list[ActiveRun]) -> list[str]:
    allocated: set[str] = set()
    for run in active_runs:
        allocated.update(run.allocated_gpu_indices)
    return sorted(allocated, key=int)


def reserve_job(
    request: JobRequest,
    active_runs: list[ActiveRun],
    managed_gpu_indices: list[str],
) -> list[str] | None:
    if any(run.exclusive for run in active_runs):
        return None
    if request.exclusive and active_runs:
        return None

    occupied = set(active_gpu_indices(active_runs))
    if request.requested_gpu_indices:
        if managed_gpu_indices:
            unknown = sorted(set(request.requested_gpu_indices) - set(managed_gpu_indices), key=int)
            if unknown:
                raise ValueError(f"requested_gpu_indices {unknown} outside managed_gpus {managed_gpu_indices}")
        if occupied.intersection(request.requested_gpu_indices):
            return None
        return request.requested_gpu_indices

    if request.gpu_count > 0:
        candidate_pool = request.allowed_gpu_indices or managed_gpu_indices
        if not candidate_pool:
            raise ValueError("gpu_count requires allow_gpu_indices or controller managed_gpus")
        free = [idx for idx in candidate_pool if idx not in occupied]
        if len(free) < request.gpu_count:
            return None
        return free[: request.gpu_count]

    return []


def fail_queued_job(layout: RuntimeLayout, spec_path: Path, *, reason: str) -> None:
    payload = read_json(spec_path)
    job_id = str(payload.get("job_id") or spec_path.stem)
    target_spec = layout.failed_dir / spec_path.name
    shutil.move(str(spec_path), str(target_spec))
    write_json(
        layout.failed_dir / f"{job_id}.result.json",
        {
            **payload,
            "finished_at_epoch": time.time(),
            "elapsed_seconds": 0.0,
            "returncode": None,
            "status": "failed",
            "failure_reason": reason,
        },
    )


def launch_job(
    layout: RuntimeLayout,
    spec_path: Path,
    request: JobRequest,
    allocated_gpu_indices: list[str],
) -> ActiveRun:
    running_path = layout.running_dir / spec_path.name
    shutil.move(str(spec_path), str(running_path))

    env = os.environ.copy()
    env.update(request.env)
    if allocated_gpu_indices:
        env["ALLOCATED_GPU_INDICES"] = ",".join(allocated_gpu_indices)
        env["ALLOCATED_GPU_COUNT"] = str(len(allocated_gpu_indices))
        if request.set_cuda_visible_devices:
            env["CUDA_VISIBLE_DEVICES"] = ",".join(allocated_gpu_indices)

    workdir = Path(request.workdir or layout.root).resolve()
    log_path = layout.jobs_log_dir / f"{request.job_id}.log"
    meta_path = layout.running_dir / f"{request.job_id}.meta.json"

    log_handle = log_path.open("a", encoding="utf-8")
    command = list(request.command)
    if command and command[0] in {"python", "python3"}:
        command[0] = shutil.which(command[0]) or sys.executable

    proc = subprocess.Popen(
        command,
        cwd=str(workdir),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    meta = {
        "job_id": request.job_id,
        "command": command,
        "workdir": str(workdir),
        "timeout_seconds": request.timeout_seconds,
        "started_at_epoch": time.time(),
        "pid": proc.pid,
        "pgid": proc.pid,
        "log_path": str(log_path),
        "running_spec_path": str(running_path),
        "allocated_gpu_indices": allocated_gpu_indices,
        "exclusive": request.exclusive,
        "gpu_count": request.gpu_count,
        "allowed_gpu_indices": request.allowed_gpu_indices,
        "requested_gpu_indices": request.requested_gpu_indices,
        "reservation_source": request.reservation_source,
        "set_cuda_visible_devices": request.set_cuda_visible_devices,
    }
    write_json(meta_path, meta)
    setattr(proc, "_log_handle", log_handle)
    setattr(proc, "_started_at_epoch", meta["started_at_epoch"])
    return ActiveRun(
        meta=meta,
        proc=proc,
        running_spec_path=running_path,
        running_meta_path=meta_path,
        allocated_gpu_indices=allocated_gpu_indices,
        exclusive=request.exclusive,
    )


def summarize_active_run(run: ActiveRun) -> dict[str, Any]:
    return {
        **run.meta,
        "allocated_gpu_indices": run.allocated_gpu_indices,
        "exclusive": run.exclusive,
    }


def finalize_job(
    layout: RuntimeLayout,
    run: ActiveRun,
    *,
    target_dir: Path | None = None,
    status: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    log_handle = getattr(run.proc, "_log_handle", None)
    if log_handle is not None and not log_handle.closed:
        log_handle.close()

    returncode = run.proc.poll()
    elapsed = time.time() - float(run.meta["started_at_epoch"])
    result_status = status or ("done" if returncode == 0 else "failed")
    result = {
        **run.meta,
        "finished_at_epoch": time.time(),
        "elapsed_seconds": elapsed,
        "returncode": returncode,
        "status": result_status,
    }
    if extra:
        result.update(extra)
    out_dir = target_dir or (layout.done_dir if result_status == "done" else layout.failed_dir)
    target_spec = out_dir / run.running_spec_path.name
    if run.running_spec_path.exists():
        shutil.move(str(run.running_spec_path), str(target_spec))
    if run.running_meta_path.exists():
        shutil.move(str(run.running_meta_path), str(out_dir / run.running_meta_path.name))
    write_json(out_dir / f"{run.meta['job_id']}.result.json", result)
    return result


def controller_payload(
    active_runs: list[ActiveRun],
    *,
    managed_gpu_indices: list[str],
    stop_when_idle: bool = False,
    last_control_action: str | None = None,
) -> dict[str, Any]:
    active_jobs = [summarize_active_run(run) for run in active_runs]
    active_job = active_jobs[0] if len(active_jobs) == 1 else None
    active_gpu = active_gpu_indices(active_runs)
    payload = {
        "updated_at_epoch": time.time(),
        "pid": os.getpid(),
        "active_job": active_job,
        "active_jobs": active_jobs,
        "active_job_count": len(active_jobs),
        "managed_gpu_indices": managed_gpu_indices,
        "controller_gpu_indices": managed_gpu_indices,
        "active_gpu_indices": active_gpu,
        "occupied_gpu_indices": active_gpu,
        "free_gpu_indices": [idx for idx in managed_gpu_indices if idx not in set(active_gpu)],
        "stop_when_idle": stop_when_idle,
    }
    if last_control_action:
        payload["last_control_action"] = last_control_action
    return payload


def terminate_process_group(proc: subprocess.Popen[str], *, signal_name: str, grace_seconds: float) -> int | None:
    if proc.poll() is not None:
        return proc.poll()
    sig = signal_from_name(signal_name)
    try:
        os.killpg(proc.pid, sig)
    except Exception:
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.send_signal(sig)
    deadline = time.time() + max(0.0, grace_seconds)
    while time.time() < deadline:
        rc = proc.poll()
        if rc is not None:
            return rc
        time.sleep(0.2)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            proc.kill()
    try:
        return proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return proc.poll()


def handle_timeout(run: ActiveRun) -> None:
    timeout_seconds = float(run.meta.get("timeout_seconds", 0) or 0)
    if timeout_seconds <= 0:
        return
    started_at = float(run.meta.get("started_at_epoch", 0) or 0)
    if started_at <= 0 or time.time() - started_at <= timeout_seconds:
        return
    terminate_process_group(run.proc, signal_name="TERM", grace_seconds=2)


def move_job_to_cancelled(layout: RuntimeLayout, spec_path: Path, *, reason: str, request_id: str) -> dict[str, Any]:
    spec = read_json(spec_path)
    job_id = str(spec.get("job_id") or spec_path.stem)
    target_spec = layout.cancelled_dir / spec_path.name
    shutil.move(str(spec_path), str(target_spec))
    result = {
        **spec,
        "finished_at_epoch": time.time(),
        "status": "cancelled",
        "cancel_reason": reason,
        "control_request_id": request_id,
    }
    write_json(layout.cancelled_dir / f"{job_id}.result.json", result)
    return result


def purge_queue(layout: RuntimeLayout, *, reason: str, request_id: str, job_ids: list[str] | None = None) -> list[str]:
    requested = set(job_ids or [])
    purged: list[str] = []
    for spec_path in queued_specs(layout):
        spec = read_json(spec_path)
        job_id = str(spec.get("job_id") or spec_path.stem)
        if requested and job_id not in requested:
            continue
        move_job_to_cancelled(layout, spec_path, reason=reason, request_id=request_id)
        purged.append(job_id)
    return purged


def write_control_result(
    layout: RuntimeLayout,
    request_path: Path,
    request: dict[str, Any],
    *,
    status: str,
    details: dict[str, Any] | None = None,
) -> None:
    target_dir = layout.control_done_dir if status in {"done", "accepted", "noop"} else layout.control_failed_dir
    target_request = target_dir / request_path.name
    if request_path.exists():
        shutil.move(str(request_path), str(target_request))
    request_id = str(request.get("request_id") or request_path.stem)
    result = {
        **request,
        "status": status,
        "finished_at_epoch": time.time(),
    }
    if details:
        result.update(details)
    write_json(target_dir / f"{request_id}.result.json", result)


def cancel_matching_active_runs(
    layout: RuntimeLayout,
    active_runs: list[ActiveRun],
    *,
    request_id: str,
    reason: str,
    signal_name: str,
    grace_seconds: float,
    job_ids: list[str],
) -> tuple[list[ActiveRun], list[str]]:
    requested = set(job_ids)
    cancelled: list[str] = []
    survivors: list[ActiveRun] = []
    cancel_all = not requested
    for run in active_runs:
        if cancel_all or run.meta["job_id"] in requested:
            returncode = terminate_process_group(run.proc, signal_name=signal_name, grace_seconds=grace_seconds)
            finalize_job(
                layout,
                run,
                target_dir=layout.failed_dir,
                status="failed",
                extra={
                    "control_request_id": request_id,
                    "stop_reason": reason,
                    "stopped_by_signal": signal_name.upper(),
                    "control_action": "cancel_active_job",
                    "returncode": returncode,
                },
            )
            cancelled.append(run.meta["job_id"])
        else:
            survivors.append(run)
    return survivors, cancelled


def process_control_request(
    layout: RuntimeLayout,
    request_path: Path,
    *,
    active_runs: list[ActiveRun],
    stop_when_idle: bool,
) -> tuple[list[ActiveRun], bool, bool, str]:
    request = read_json(request_path)
    action = str(request.get("action") or "").strip().lower()
    request_id = str(request.get("request_id") or request_path.stem)
    reason = str(request.get("reason") or action or request_id)
    signal_name = str(request.get("signal") or "TERM")
    grace_seconds = float(request.get("grace_seconds", 15.0) or 15.0)
    job_ids = string_list(request.get("job_ids"))
    last_control_action = action or "invalid"
    should_exit = False

    if action == "cancel_active_job":
        active_runs, cancelled = cancel_matching_active_runs(
            layout,
            active_runs,
            request_id=request_id,
            reason=reason,
            signal_name=signal_name,
            grace_seconds=grace_seconds,
            job_ids=job_ids,
        )
        write_control_result(
            layout,
            request_path,
            request,
            status="done" if cancelled else "noop",
            details={"cancelled_job_ids": cancelled, "cancelled_job_count": len(cancelled)},
        )
        return active_runs, stop_when_idle, should_exit, last_control_action

    if action == "purge_queue":
        purged = purge_queue(layout, reason=reason, request_id=request_id, job_ids=job_ids)
        write_control_result(
            layout,
            request_path,
            request,
            status="done",
            details={"purged_job_ids": purged, "purged_job_count": len(purged)},
        )
        return active_runs, stop_when_idle, should_exit, last_control_action

    if action in {"stop_controller", "drain_and_stop", "retire_controller"}:
        purge_before_stop = bool_value(request.get("purge_queue"), default=action == "retire_controller")
        cancel_before_stop = bool_value(request.get("cancel_active_job"), default=action == "retire_controller")
        details: dict[str, Any] = {}
        if purge_before_stop:
            purged = purge_queue(layout, reason=reason, request_id=request_id, job_ids=job_ids)
            details["purged_job_ids"] = purged
            details["purged_job_count"] = len(purged)
        if cancel_before_stop:
            active_runs, cancelled = cancel_matching_active_runs(
                layout,
                active_runs,
                request_id=request_id,
                reason=reason,
                signal_name=signal_name,
                grace_seconds=grace_seconds,
                job_ids=job_ids,
            )
            details["cancelled_job_ids"] = cancelled
            details["cancelled_job_count"] = len(cancelled)
        stop_when_idle = True
        should_exit = not active_runs
        details["stop_when_idle"] = stop_when_idle
        details["controller_will_exit_now"] = should_exit
        write_control_result(layout, request_path, request, status="accepted", details=details)
        return active_runs, stop_when_idle, should_exit, last_control_action

    write_control_result(
        layout,
        request_path,
        request,
        status="failed",
        details={"message": f"unsupported action: {action!r}"},
    )
    return active_runs, stop_when_idle, should_exit, last_control_action


def harvest_finished_runs(layout: RuntimeLayout, active_runs: list[ActiveRun]) -> list[ActiveRun]:
    survivors: list[ActiveRun] = []
    for run in active_runs:
        handle_timeout(run)
        rc = run.proc.poll()
        if rc is None:
            survivors.append(run)
            continue
        finalize_job(layout, run)
        append_jsonl(
            layout.controller_heartbeat,
            {
                "event": "job_finished",
                "job_id": run.meta["job_id"],
                "returncode": rc,
                "allocated_gpu_indices": run.allocated_gpu_indices,
                "updated_at_epoch": time.time(),
            },
        )
    return survivors


def dispatch_launchable_jobs(
    layout: RuntimeLayout,
    active_runs: list[ActiveRun],
    managed_gpu_indices: list[str],
    *,
    max_new_jobs: int | None = None,
) -> list[ActiveRun]:
    launched = 0
    for spec_path in queued_specs(layout):
        if max_new_jobs is not None and launched >= max_new_jobs:
            break
        try:
            request = load_job_request(spec_path)
            allocated = reserve_job(request, active_runs, managed_gpu_indices)
        except ValueError as exc:
            fail_queued_job(layout, spec_path, reason=str(exc))
            append_jsonl(
                layout.controller_heartbeat,
                {
                    "event": "job_rejected",
                    "spec_path": str(spec_path),
                    "reason": str(exc),
                    "updated_at_epoch": time.time(),
                },
            )
            continue
        if allocated is None:
            continue
        run = launch_job(layout, spec_path, request, allocated)
        active_runs.append(run)
        launched += 1
        append_jsonl(
            layout.controller_heartbeat,
            {
                "event": "job_started",
                "job_id": run.meta["job_id"],
                "pid": run.proc.pid,
                "allocated_gpu_indices": allocated,
                "reservation_source": run.meta["reservation_source"],
                "updated_at_epoch": time.time(),
            },
        )
        if run.exclusive:
            break
    return active_runs


def main() -> int:
    args = parse_args()
    layout = build_layout(args.root.resolve())
    managed_gpu_indices = detect_managed_gpu_indices(args.managed_gpu_indices)
    active_runs: list[ActiveRun] = []
    last_heartbeat = 0.0
    stop_when_idle = False
    last_control_action: str | None = None
    launched_any = False

    while True:
        active_runs = harvest_finished_runs(layout, active_runs)

        while True:
            control_path = next_control_request(layout)
            if control_path is None:
                break
            active_runs, stop_when_idle, should_exit, last_control_action = process_control_request(
                layout,
                control_path,
                active_runs=active_runs,
                stop_when_idle=stop_when_idle,
            )
            if should_exit and not active_runs:
                payload = controller_payload(
                    active_runs,
                    managed_gpu_indices=managed_gpu_indices,
                    stop_when_idle=stop_when_idle,
                    last_control_action=last_control_action,
                )
                write_json(layout.controller_state, payload)
                append_jsonl(
                    layout.controller_heartbeat,
                    {
                        **payload,
                        "event": "controller_stopped",
                        "reason": "control_request",
                    },
                )
                return 0

        if not stop_when_idle:
            active_runs = dispatch_launchable_jobs(
                layout,
                active_runs,
                managed_gpu_indices,
                max_new_jobs=1 if args.once and not launched_any else None,
            )
            if active_runs:
                launched_any = True

        now = time.time()
        if now - last_heartbeat >= args.heartbeat_seconds:
            payload = controller_payload(
                active_runs,
                managed_gpu_indices=managed_gpu_indices,
                stop_when_idle=stop_when_idle,
                last_control_action=last_control_action,
            )
            write_json(layout.controller_state, payload)
            append_jsonl(layout.controller_heartbeat, payload)
            last_heartbeat = now

        if stop_when_idle and not active_runs:
            append_jsonl(
                layout.controller_heartbeat,
                {
                    "event": "controller_stopped",
                    "reason": "stop_when_idle",
                    "updated_at_epoch": time.time(),
                    "pid": os.getpid(),
                },
            )
            break

        if args.once and launched_any and not active_runs:
            break

        time.sleep(max(0.2, args.poll_seconds))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
