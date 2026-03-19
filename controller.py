#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
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


def signal_from_name(name: str) -> signal.Signals:
    normalized = name.strip().upper()
    mapping = {
        "INT": signal.SIGINT,
        "KILL": signal.SIGKILL,
        "QUIT": signal.SIGQUIT,
        "TERM": signal.SIGTERM,
    }
    return mapping.get(normalized, signal.SIGTERM)


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


def next_job(layout: RuntimeLayout) -> Path | None:
    return next_visible_json(layout.queue_dir)


def next_control_request(layout: RuntimeLayout) -> Path | None:
    return next_visible_json(layout.control_queue_dir)


def load_job(spec_path: Path) -> dict[str, Any]:
    spec = read_json(spec_path)
    if "job_id" not in spec:
        raise ValueError(f"{spec_path} missing required field job_id")
    if "command" not in spec:
        raise ValueError(f"{spec_path} missing required field command")
    if not isinstance(spec["command"], list) or not spec["command"]:
        raise ValueError(f"{spec_path} field command must be a non-empty list")
    return spec


def launch_job(layout: RuntimeLayout, spec_path: Path) -> tuple[dict[str, Any], subprocess.Popen[str], Path]:
    spec = load_job(spec_path)
    job_id = str(spec["job_id"])
    running_path = layout.running_dir / spec_path.name
    shutil.move(str(spec_path), str(running_path))

    env = os.environ.copy()
    env.update({str(k): str(v) for k, v in dict(spec.get("env", {})).items()})

    workdir = Path(str(spec.get("workdir", layout.root))).resolve()
    timeout_seconds = float(spec.get("timeout_seconds", 0) or 0)
    log_path = layout.jobs_log_dir / f"{job_id}.log"
    meta_path = layout.running_dir / f"{job_id}.meta.json"

    log_handle = log_path.open("a", encoding="utf-8")
    command = [str(x) for x in spec["command"]]
    if command and command[0] in {"python", "python3"}:
        resolved = shutil.which(command[0]) or sys.executable
        command[0] = resolved

    proc = subprocess.Popen(
        command,
        cwd=str(workdir),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    meta = {
        "job_id": job_id,
        "command": command,
        "workdir": str(workdir),
        "timeout_seconds": timeout_seconds,
        "started_at_epoch": time.time(),
        "pid": proc.pid,
        "log_path": str(log_path),
        "running_spec_path": str(running_path),
    }
    write_json(meta_path, meta)
    setattr(proc, "_log_handle", log_handle)
    return meta, proc, running_path


def finalize_job(
    layout: RuntimeLayout,
    meta: dict[str, Any],
    proc: subprocess.Popen[str],
    running_spec_path: Path,
    *,
    target_dir: Path | None = None,
    status: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    log_handle = getattr(proc, "_log_handle", None)
    if log_handle is not None and not log_handle.closed:
        log_handle.close()

    job_id = str(meta["job_id"])
    returncode = proc.poll()
    elapsed = time.time() - float(meta["started_at_epoch"])
    result_status = status or ("done" if returncode == 0 else "failed")
    result = {
        **meta,
        "finished_at_epoch": time.time(),
        "elapsed_seconds": elapsed,
        "returncode": returncode,
        "status": result_status,
    }
    if extra:
        result.update(extra)
    out_dir = target_dir or (layout.done_dir if result_status == "done" else layout.failed_dir)
    target_spec = out_dir / running_spec_path.name
    if running_spec_path.exists():
        shutil.move(str(running_spec_path), str(target_spec))
    write_json(out_dir / f"{job_id}.result.json", result)
    return result


def controller_payload(
    active: dict[str, Any] | None,
    *,
    stop_when_idle: bool = False,
    last_control_action: str | None = None,
) -> dict[str, Any]:
    payload = {
        "updated_at_epoch": time.time(),
        "pid": os.getpid(),
        "active_job": active,
        "stop_when_idle": stop_when_idle,
    }
    if last_control_action:
        payload["last_control_action"] = last_control_action
    return payload


def handle_timeout(proc: subprocess.Popen[str], timeout_seconds: float) -> None:
    if timeout_seconds <= 0:
        return
    started_at = getattr(proc, "_started_at_epoch", None)
    if started_at is None:
        return
    if time.time() - started_at <= timeout_seconds:
        return
    proc.terminate()
    time.sleep(2)
    if proc.poll() is None:
        proc.kill()


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


def purge_queue(
    layout: RuntimeLayout,
    *,
    reason: str,
    request_id: str,
    job_ids: list[str] | None = None,
) -> list[str]:
    selected: list[str] = []
    requested = set(job_ids or [])
    for spec_path in sorted(layout.queue_dir.glob("*.json")):
        spec = read_json(spec_path)
        job_id = str(spec.get("job_id") or spec_path.stem)
        if requested and job_id not in requested:
            continue
        move_job_to_cancelled(layout, spec_path, reason=reason, request_id=request_id)
        selected.append(job_id)
    return selected


def terminate_process(proc: subprocess.Popen[str], *, signal_name: str, grace_seconds: float) -> int | None:
    if proc.poll() is not None:
        return proc.poll()
    sig = signal_from_name(signal_name)
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
        proc.kill()
    try:
        return proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return proc.poll()


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


def cancel_active_job(
    layout: RuntimeLayout,
    *,
    active_meta: dict[str, Any],
    active_proc: subprocess.Popen[str],
    active_running_path: Path,
    request_id: str,
    reason: str,
    signal_name: str,
    grace_seconds: float,
) -> tuple[dict[str, Any], None, None, dict[str, Any]]:
    returncode = terminate_process(active_proc, signal_name=signal_name, grace_seconds=grace_seconds)
    extra = {
        "control_request_id": request_id,
        "stop_reason": reason,
        "stopped_by_signal": signal_name.upper(),
        "control_action": "cancel_active_job",
    }
    result = finalize_job(
        layout,
        active_meta,
        active_proc,
        active_running_path,
        target_dir=layout.failed_dir,
        status="failed",
        extra=extra | {"returncode": returncode},
    )
    return {}, None, None, result


def process_control_request(
    layout: RuntimeLayout,
    request_path: Path,
    *,
    active_meta: dict[str, Any] | None,
    active_proc: subprocess.Popen[str] | None,
    active_running_path: Path | None,
    stop_when_idle: bool,
) -> tuple[dict[str, Any] | None, subprocess.Popen[str] | None, Path | None, bool, bool, str]:
    request = read_json(request_path)
    action = str(request.get("action") or "").strip().lower()
    request_id = str(request.get("request_id") or request_path.stem)
    reason = str(request.get("reason") or action or request_id)
    signal_name = str(request.get("signal") or "TERM")
    grace_seconds = float(request.get("grace_seconds", 15.0) or 15.0)
    requested_job_ids = string_list(request.get("job_ids"))
    should_exit = False
    last_control_action = action or "invalid"

    if action == "cancel_active_job":
        if active_proc is None or active_meta is None or active_running_path is None:
            write_control_result(
                layout,
                request_path,
                request,
                status="noop",
                details={"message": "no active job to cancel"},
            )
            return active_meta, active_proc, active_running_path, stop_when_idle, should_exit, last_control_action
        active_meta, active_proc, active_running_path, result = cancel_active_job(
            layout,
            active_meta=active_meta,
            active_proc=active_proc,
            active_running_path=active_running_path,
            request_id=request_id,
            reason=reason,
            signal_name=signal_name,
            grace_seconds=grace_seconds,
        )
        write_control_result(
            layout,
            request_path,
            request,
            status="done",
            details={"cancelled_job_id": result["job_id"], "returncode": result["returncode"]},
        )
        return active_meta, active_proc, active_running_path, stop_when_idle, should_exit, last_control_action

    if action == "purge_queue":
        purged_job_ids = purge_queue(
            layout,
            reason=reason,
            request_id=request_id,
            job_ids=requested_job_ids,
        )
        write_control_result(
            layout,
            request_path,
            request,
            status="done",
            details={"purged_job_ids": purged_job_ids, "purged_job_count": len(purged_job_ids)},
        )
        return active_meta, active_proc, active_running_path, stop_when_idle, should_exit, last_control_action

    if action in {"stop_controller", "drain_and_stop", "retire_controller"}:
        purge_before_stop = bool_value(request.get("purge_queue"), default=action == "retire_controller")
        cancel_before_stop = bool_value(request.get("cancel_active_job"), default=action == "retire_controller")
        details: dict[str, Any] = {}
        if purge_before_stop:
            purged_job_ids = purge_queue(
                layout,
                reason=reason,
                request_id=request_id,
                job_ids=requested_job_ids,
            )
            details["purged_job_ids"] = purged_job_ids
            details["purged_job_count"] = len(purged_job_ids)
        if cancel_before_stop and active_proc is not None and active_meta is not None and active_running_path is not None:
            active_meta, active_proc, active_running_path, result = cancel_active_job(
                layout,
                active_meta=active_meta,
                active_proc=active_proc,
                active_running_path=active_running_path,
                request_id=request_id,
                reason=reason,
                signal_name=signal_name,
                grace_seconds=grace_seconds,
            )
            details["cancelled_job_id"] = result["job_id"]
            details["cancelled_returncode"] = result["returncode"]
        stop_when_idle = True
        should_exit = active_proc is None
        details["stop_when_idle"] = stop_when_idle
        details["controller_will_exit_now"] = should_exit
        write_control_result(layout, request_path, request, status="accepted", details=details)
        return active_meta, active_proc, active_running_path, stop_when_idle, should_exit, last_control_action

    write_control_result(
        layout,
        request_path,
        request,
        status="failed",
        details={"message": f"unsupported action: {action!r}"},
    )
    return active_meta, active_proc, active_running_path, stop_when_idle, should_exit, last_control_action


def main() -> int:
    args = parse_args()
    layout = build_layout(args.root.resolve())
    active_meta: dict[str, Any] | None = None
    active_proc: subprocess.Popen[str] | None = None
    active_running_path: Path | None = None
    last_heartbeat = 0.0
    stop_when_idle = False
    last_control_action: str | None = None

    while True:
        while True:
            control_path = next_control_request(layout)
            if control_path is None:
                break
            (
                active_meta,
                active_proc,
                active_running_path,
                stop_when_idle,
                should_exit,
                last_control_action,
            ) = process_control_request(
                layout,
                control_path,
                active_meta=active_meta,
                active_proc=active_proc,
                active_running_path=active_running_path,
                stop_when_idle=stop_when_idle,
            )
            if should_exit and active_proc is None:
                payload = controller_payload(
                    active_meta,
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

        now = time.time()
        if now - last_heartbeat >= args.heartbeat_seconds:
            payload = controller_payload(
                active_meta,
                stop_when_idle=stop_when_idle,
                last_control_action=last_control_action,
            )
            write_json(layout.controller_state, payload)
            append_jsonl(layout.controller_heartbeat, payload)
            last_heartbeat = now

        if active_proc is None:
            if stop_when_idle:
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
            spec_path = next_job(layout)
            if spec_path is not None:
                active_meta, active_proc, active_running_path = launch_job(layout, spec_path)
                setattr(active_proc, "_started_at_epoch", active_meta["started_at_epoch"])
                append_jsonl(
                    layout.controller_heartbeat,
                    {
                        "event": "job_started",
                        "job_id": active_meta["job_id"],
                        "pid": active_proc.pid,
                        "updated_at_epoch": time.time(),
                    },
                )
            else:
                if args.once:
                    break
                time.sleep(max(0.2, args.poll_seconds))
                continue

        if active_proc is not None and active_meta is not None and active_running_path is not None:
            handle_timeout(active_proc, float(active_meta.get("timeout_seconds", 0) or 0))
            rc = active_proc.poll()
            if rc is not None:
                finalize_job(layout, active_meta, active_proc, active_running_path)
                append_jsonl(
                    layout.controller_heartbeat,
                    {
                        "event": "job_finished",
                        "job_id": active_meta["job_id"],
                        "returncode": rc,
                        "updated_at_epoch": time.time(),
                    },
                )
                active_meta = None
                active_proc = None
                active_running_path = None
                if args.once:
                    break
            else:
                time.sleep(max(0.2, args.poll_seconds))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
