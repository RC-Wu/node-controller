#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
import shutil
import signal
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


@dataclass
class RuntimeLayout:
    root: Path
    queue_dir: Path
    running_dir: Path
    done_dir: Path
    failed_dir: Path
    logs_dir: Path
    jobs_log_dir: Path
    state_dir: Path
    controller_state: Path
    controller_heartbeat: Path


def build_layout(root: Path) -> RuntimeLayout:
    jobs_root = root / "jobs"
    logs_root = root / "logs"
    state_root = root / "state"
    layout = RuntimeLayout(
        root=root,
        queue_dir=jobs_root / "queue",
        running_dir=jobs_root / "running",
        done_dir=jobs_root / "done",
        failed_dir=jobs_root / "failed",
        logs_dir=logs_root,
        jobs_log_dir=logs_root / "jobs",
        state_dir=state_root,
        controller_state=state_root / "controller_state.json",
        controller_heartbeat=logs_root / "controller_heartbeat.jsonl",
    )
    for directory in [
        layout.queue_dir,
        layout.running_dir,
        layout.done_dir,
        layout.failed_dir,
        layout.jobs_log_dir,
        layout.state_dir,
    ]:
        directory.mkdir(parents=True, exist_ok=True)
    # Let the dev-machine user enqueue specs and inspect outputs over vePFS
    # while the controller itself keeps ownership of process-local writes.
    try:
        os.chmod(layout.root, 0o755)
        os.chmod(jobs_root, 0o755)
        os.chmod(logs_root, 0o755)
        os.chmod(state_root, 0o755)
        os.chmod(layout.queue_dir, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO | stat.S_ISVTX)
        os.chmod(layout.running_dir, 0o755)
        os.chmod(layout.done_dir, 0o755)
        os.chmod(layout.failed_dir, 0o755)
        os.chmod(layout.jobs_log_dir, 0o755)
    except PermissionError:
        pass
    return layout


def next_job(layout: RuntimeLayout) -> Path | None:
    specs = sorted(layout.queue_dir.glob("*.json"))
    return specs[0] if specs else None


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


def finalize_job(layout: RuntimeLayout, meta: dict[str, Any], proc: subprocess.Popen[str], running_spec_path: Path) -> None:
    log_handle = getattr(proc, "_log_handle", None)
    if log_handle is not None and not log_handle.closed:
        log_handle.close()

    job_id = str(meta["job_id"])
    returncode = proc.poll()
    elapsed = time.time() - float(meta["started_at_epoch"])
    result = {
        **meta,
        "finished_at_epoch": time.time(),
        "elapsed_seconds": elapsed,
        "returncode": returncode,
        "status": "done" if returncode == 0 else "failed",
    }
    target_dir = layout.done_dir if returncode == 0 else layout.failed_dir
    target_spec = target_dir / running_spec_path.name
    if running_spec_path.exists():
        shutil.move(str(running_spec_path), str(target_spec))
    write_json(target_dir / f"{job_id}.result.json", result)


def controller_payload(active: dict[str, Any] | None) -> dict[str, Any]:
    payload = {
        "updated_at_epoch": time.time(),
        "pid": os.getpid(),
        "active_job": active,
    }
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


def main() -> int:
    args = parse_args()
    layout = build_layout(args.root.resolve())
    active_meta: dict[str, Any] | None = None
    active_proc: subprocess.Popen[str] | None = None
    active_running_path: Path | None = None
    last_heartbeat = 0.0

    while True:
        now = time.time()
        if now - last_heartbeat >= args.heartbeat_seconds:
            payload = controller_payload(active_meta)
            write_json(layout.controller_state, payload)
            append_jsonl(layout.controller_heartbeat, payload)
            last_heartbeat = now

        if active_proc is None:
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
                if args.once:
                    # still let the job finish in the current process
                    pass
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

__CODEX_REMOTE_EOF__
