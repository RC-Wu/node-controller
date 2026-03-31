#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO


SCHEMA_VERSION = 2
SUPERVISOR_SCRIPT = Path(__file__).with_name("job_supervisor.py")


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
    ap.add_argument(
        "--supervisor-grace-seconds",
        type=float,
        default=15.0,
        help="Grace period when asking a job supervisor to stop its child process group.",
    )
    ap.add_argument(
        "--enable-compat-kill-queue",
        action="store_true",
        help="Re-enable legacy jobs/kill compatibility processing. Disabled by default for safety/auditability.",
    )
    ap.add_argument(
        "--foreign-gpu-memory-threshold-mb",
        type=int,
        default=2048,
        help="Treat non-controller GPU usage above this memory threshold as externally blocked for scheduling.",
    )
    ap.add_argument(
        "--startup-min-schedulable-gpu-count",
        type=int,
        default=0,
        help="If no jobs were reattached on boot, require at least this many schedulable GPUs or exit non-zero.",
    )
    ap.add_argument("--once", action="store_true", help="Process at most one visible queue item, then exit.")
    return ap.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_safe(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = read_json(path)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return None, f"expected a JSON object, got {type(payload).__name__}"
    return payload, None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def append_text_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


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
        "KILL": getattr(signal, "SIGKILL", signal.SIGTERM),
        "QUIT": getattr(signal, "SIGQUIT", signal.SIGTERM),
        "TERM": signal.SIGTERM,
    }
    return mapping.get(normalized, signal.SIGTERM)


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            access = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
            handle = ctypes.windll.kernel32.OpenProcess(access, False, pid)
        except Exception:
            return False
        if handle:
            try:
                exit_code = ctypes.c_ulong()
                if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return int(exit_code.value) == 259  # STILL_ACTIVE
                return True
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        return int(ctypes.windll.kernel32.GetLastError()) == 5
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    except SystemError:
        return False
    return True


def read_process_start_time_ticks(pid: int) -> int | None:
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        text = stat_path.read_text(encoding="utf-8")
    except Exception:
        return None
    try:
        tail = text[text.rfind(")") + 2 :].split()
        return int(tail[19])
    except Exception:
        return None


def pid_matches_command(pid: int, expected_command: list[str]) -> bool:
    cmdline_path = Path("/proc") / str(pid) / "cmdline"
    try:
        cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace")
    except Exception:
        return True
    if not expected_command:
        return True
    expected_tokens = [Path(part).name for part in expected_command[:3] if str(part).strip()]
    if not expected_tokens:
        return True
    haystack = cmdline.lower()
    return any(token.lower() in haystack for token in expected_tokens)


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


def probe_gpu_status(managed_gpu_indices: list[str]) -> list[dict[str, Any]]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []

    managed = set(managed_gpu_indices)
    rows: list[dict[str, Any]] = []
    for raw_line in result.stdout.splitlines():
        parts = [part.strip() for part in raw_line.split(",")]
        if len(parts) < 5 or not parts[0].isdigit():
            continue
        index = str(int(parts[0]))
        if managed and index not in managed:
            continue

        def maybe_int(text: str) -> int | None:
            try:
                return int(text)
            except Exception:
                return None

        rows.append(
            {
                "index": int(index),
                "util_pct": maybe_int(parts[1]),
                "mem_used_mb": maybe_int(parts[2]),
                "mem_total_mb": maybe_int(parts[3]),
                "temp_c": maybe_int(parts[4]),
            }
        )
    return rows


def read_process_group_id(pid: int) -> int | None:
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        text = stat_path.read_text(encoding="utf-8")
    except Exception:
        return None
    try:
        tail = text[text.rfind(")") + 2 :].split()
        return int(tail[2])
    except Exception:
        return None


def probe_gpu_processes(managed_gpu_indices: list[str], active_runs: list["ActiveRun"]) -> list[dict[str, Any]]:
    try:
        gpu_result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid",
                "--format=csv,noheader",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        proc_result = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []

    managed = set(managed_gpu_indices)
    gpu_uuid_to_index: dict[str, str] = {}
    for raw_line in gpu_result.stdout.splitlines():
        parts = [part.strip() for part in raw_line.split(",")]
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        index = str(int(parts[0]))
        if managed and index not in managed:
            continue
        gpu_uuid_to_index[parts[1]] = index

    pgid_to_job: dict[int, dict[str, Any]] = {}
    for run in active_runs:
        tracked_pgid = int(run.meta.get("child_pgid") or run.proc.pgid or 0)
        if tracked_pgid <= 0:
            tracked_pgid = run.proc.pgid
        pgid_to_job[tracked_pgid] = {
            "job_id": str(run.meta.get("job_id", "")),
            "allocated_gpu_indices": list(run.allocated_gpu_indices),
            "controller_pid": int(run.meta.get("child_pid") or run.proc.pid or 0),
            "controller_pgid": tracked_pgid,
        }

    rows: list[dict[str, Any]] = []
    for raw_line in proc_result.stdout.splitlines():
        text = raw_line.strip()
        if not text or "No running processes found" in text:
            continue
        parts = [part.strip() for part in raw_line.split(",")]
        if len(parts) < 4:
            continue
        gpu_uuid = parts[0]
        gpu_index = gpu_uuid_to_index.get(gpu_uuid)
        if gpu_index is None:
            continue
        try:
            pid = int(parts[1])
        except Exception:
            continue
        pgid = read_process_group_id(pid)
        controller_job = pgid_to_job.get(pgid or -1)
        try:
            used_memory_mb = int(parts[3])
        except Exception:
            used_memory_mb = None
        rows.append(
            {
                "gpu_index": int(gpu_index),
                "gpu_uuid": gpu_uuid,
                "pid": pid,
                "pgid": pgid,
                "process_name": parts[2],
                "used_memory_mb": used_memory_mb,
                "controller_job_id": None if controller_job is None else controller_job["job_id"],
                "controller_allocated_gpu_indices": [] if controller_job is None else controller_job["allocated_gpu_indices"],
                "controller_pid": None if controller_job is None else controller_job["controller_pid"],
                "controller_pgid": None if controller_job is None else controller_job["controller_pgid"],
            }
        )
    return sorted(rows, key=lambda item: (item["gpu_index"], item["pid"]))


def count_runtime_specs(path: Path) -> int:
    return sum(
        1
        for item in path.glob("*.json")
        if not item.name.endswith(".meta.json") and not item.name.endswith(".result.json")
    )


def count_runtime_results(path: Path) -> int:
    return sum(1 for _ in path.glob("*.result.json"))


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
    shared_root: Path
    queue_dir: Path
    running_dir: Path
    done_dir: Path
    failed_dir: Path
    cancelled_dir: Path
    kill_dir: Path
    logs_dir: Path
    jobs_log_dir: Path
    state_dir: Path
    controller_state: Path
    controller_heartbeat: Path
    controller_events: Path
    controller_log: Path
    controller_admin_audit: Path
    control_dir: Path
    control_queue_dir: Path
    control_done_dir: Path
    control_failed_dir: Path
    controller_lock_dir: Path
    controller_lease_path: Path
    shared_state_dir: Path
    shared_controller_state: Path
    shared_controller_lease_path: Path
    current_runtime_pointer: Path


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
    proc: "ManagedProcess"
    running_spec_path: Path
    running_meta_path: Path
    allocated_gpu_indices: list[str]
    exclusive: bool
    job_log_path: Path | None = None
    job_event_path: Path | None = None
    log_handle: TextIO | None = None
    wrapper_status_path: Path | None = None
    attached: bool = False


@dataclass
class ManagedProcess:
    pid: int
    pgid: int
    popen: subprocess.Popen[str] | None = None
    start_time_ticks: int | None = None

    @classmethod
    def from_popen(cls, proc: subprocess.Popen[str]) -> "ManagedProcess":
        return cls(
            pid=proc.pid,
            pgid=proc.pid,
            popen=proc,
            start_time_ticks=read_process_start_time_ticks(proc.pid),
        )

    @classmethod
    def attach(cls, pid: int, pgid: int | None = None, *, start_time_ticks: int | None = None) -> "ManagedProcess":
        return cls(
            pid=pid,
            pgid=pgid or pid,
            popen=None,
            start_time_ticks=start_time_ticks,
        )

    def _attached_is_running(self) -> bool:
        if not process_exists(self.pid):
            return False
        if self.start_time_ticks is None:
            return True
        current_ticks = read_process_start_time_ticks(self.pid)
        return current_ticks is not None and current_ticks == self.start_time_ticks

    def poll(self) -> int | None:
        if self.popen is not None:
            return self.popen.poll()
        return None if self._attached_is_running() else -1

    def wait(self, timeout: float | None = None) -> int:
        if self.popen is not None:
            return self.popen.wait(timeout=timeout)
        deadline = None if timeout is None else time.time() + timeout
        while True:
            rc = self.poll()
            if rc is not None:
                return rc
            if deadline is not None and time.time() >= deadline:
                raise subprocess.TimeoutExpired(cmd=f"<attached:{self.pid}>", timeout=timeout)
            time.sleep(0.2)


def build_layout(root: Path) -> RuntimeLayout:
    jobs_root = root / "jobs"
    logs_root = root / "logs"
    state_root = root / "state"
    control_root = root / "control"
    shared_root = root.parent
    shared_state_dir = shared_root / "state"
    layout = RuntimeLayout(
        root=root,
        shared_root=shared_root,
        queue_dir=jobs_root / "queue",
        running_dir=jobs_root / "running",
        done_dir=jobs_root / "done",
        failed_dir=jobs_root / "failed",
        cancelled_dir=jobs_root / "cancelled",
        kill_dir=jobs_root / "kill",
        logs_dir=logs_root,
        jobs_log_dir=logs_root / "jobs",
        state_dir=state_root,
        controller_state=state_root / "controller_state.json",
        controller_heartbeat=logs_root / "controller_heartbeat.jsonl",
        controller_events=logs_root / "controller_events.jsonl",
        controller_log=logs_root / "controller.log",
        controller_admin_audit=logs_root / "controller_admin_audit.jsonl",
        control_dir=control_root,
        control_queue_dir=control_root / "queue",
        control_done_dir=control_root / "done",
        control_failed_dir=control_root / "failed",
        controller_lock_dir=state_root / "controller.lock",
        controller_lease_path=state_root / "controller_lease.json",
        shared_state_dir=shared_state_dir,
        shared_controller_state=shared_state_dir / "controller_state.json",
        shared_controller_lease_path=shared_state_dir / "controller_lease.json",
        current_runtime_pointer=shared_root / "current_runtime.json",
    )
    for directory in [
        layout.queue_dir,
        layout.running_dir,
        layout.done_dir,
        layout.failed_dir,
        layout.cancelled_dir,
        layout.kill_dir,
        layout.jobs_log_dir,
        layout.state_dir,
        layout.control_queue_dir,
        layout.control_done_dir,
        layout.control_failed_dir,
        layout.shared_state_dir,
    ]:
        directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(layout.root, 0o755)
        os.chmod(jobs_root, 0o755)
        os.chmod(logs_root, 0o755)
        os.chmod(state_root, 0o755)
        os.chmod(control_root, 0o755)
        os.chmod(layout.shared_state_dir, 0o755)
        os.chmod(layout.queue_dir, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO | stat.S_ISVTX)
        os.chmod(layout.control_queue_dir, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO | stat.S_ISVTX)
        os.chmod(layout.kill_dir, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO | stat.S_ISVTX)
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


def append_controller_event(
    layout: RuntimeLayout,
    event: str,
    *,
    message: str = "",
    audit: bool = False,
    **fields: Any,
) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "event": event,
        "updated_at_epoch": time.time(),
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "runtime_root": str(layout.root),
        **fields,
    }
    append_jsonl(layout.controller_events, payload)
    if audit:
        append_jsonl(layout.controller_admin_audit, payload)
    line = f"[{event}]"
    if message:
        line += f" {message}"
    elif fields:
        line += f" {json.dumps(fields, ensure_ascii=False, sort_keys=True)}"
    append_text_log(layout.controller_log, line)


def runtime_pointer_payload(
    layout: RuntimeLayout,
    controller_state_payload: dict[str, Any],
    *,
    acquired_at_epoch: float,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "runtime_root": str(layout.root),
        "runtime_name": layout.root.name,
        "shared_root": str(layout.shared_root),
        "controller_state_path": str(layout.controller_state),
        "controller_lease_path": str(layout.controller_lease_path),
        "controller_heartbeat_path": str(layout.controller_heartbeat),
        "controller_events_path": str(layout.controller_events),
        "controller_log_path": str(layout.controller_log),
        "controller_admin_audit_path": str(layout.controller_admin_audit),
        "shared_controller_state_path": str(layout.shared_controller_state),
        "shared_controller_lease_path": str(layout.shared_controller_lease_path),
        "updated_at_epoch": time.time(),
        "controller_started_at_epoch": acquired_at_epoch,
        "hostname": controller_state_payload.get("hostname"),
        "pid": controller_state_payload.get("pid"),
        "active_job_count": controller_state_payload.get("active_job_count"),
        "queue_count": controller_state_payload.get("queue_count"),
        "running_count": controller_state_payload.get("running_count"),
        "done_count": controller_state_payload.get("done_count"),
        "failed_count": controller_state_payload.get("failed_count"),
        "cancelled_count": controller_state_payload.get("cancelled_count"),
        "stop_when_idle": controller_state_payload.get("stop_when_idle"),
        "last_control_action": controller_state_payload.get("last_control_action"),
    }


def write_controller_state_snapshots(
    layout: RuntimeLayout,
    payload: dict[str, Any],
    *,
    acquired_at_epoch: float,
) -> None:
    write_json(layout.controller_state, payload)
    write_json(layout.shared_controller_state, payload)
    write_json(
        layout.current_runtime_pointer,
        runtime_pointer_payload(layout, payload, acquired_at_epoch=acquired_at_epoch),
    )

def write_failure_result(
    out_dir: Path,
    *,
    item_id: str,
    payload: dict[str, Any] | None,
    status: str,
    reason: str,
    source_path: Path,
    extra: dict[str, Any] | None = None,
) -> None:
    result = dict(payload or {})
    result.update(
        {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "failure_reason": reason,
            "source_path": str(source_path),
            "finished_at_epoch": time.time(),
        }
    )
    if extra:
        result.update(extra)
    write_json(out_dir / f"{item_id}.result.json", result)


def move_invalid_item(
    path: Path,
    *,
    out_dir: Path,
    item_id: str,
    payload: dict[str, Any] | None,
    status: str,
    reason: str,
    extra: dict[str, Any] | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / path.name
    if path.exists():
        shutil.move(str(path), str(target))
    write_failure_result(
        out_dir,
        item_id=item_id,
        payload=payload,
        status=status,
        reason=reason,
        source_path=path,
        extra=extra,
    )


def next_visible_json(path: Path) -> Path | None:
    specs = sorted(path.glob("*.json"))
    return specs[0] if specs else None


def next_control_request(layout: RuntimeLayout) -> Path | None:
    return next_visible_json(layout.control_queue_dir)


def load_job_request_payload(payload: dict[str, Any], *, source_path: Path) -> JobRequest:
    if "job_id" not in payload:
        raise ValueError(f"{source_path} missing required field job_id")
    if "command" not in payload:
        raise ValueError(f"{source_path} missing required field command")
    command = payload["command"]
    if not isinstance(command, list) or not command:
        raise ValueError(f"{source_path} field command must be a non-empty list")
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


def load_job_request(spec_path: Path) -> JobRequest:
    payload = read_json(spec_path)
    return load_job_request_payload(payload, source_path=spec_path)


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


def summarize_gpu_availability(
    managed_gpu_indices: list[str],
    active_runs: list["ActiveRun"],
    *,
    gpu_status: list[dict[str, Any]] | None = None,
    gpu_processes: list[dict[str, Any]] | None = None,
    foreign_gpu_memory_threshold_mb: int = 2048,
) -> dict[str, Any]:
    active_gpu = set(active_gpu_indices(active_runs))
    proc_by_gpu: dict[str, list[dict[str, Any]]] = {}
    for proc in gpu_processes or []:
        if not isinstance(proc, dict):
            continue
        proc_by_gpu.setdefault(str(proc.get("gpu_index")), []).append(proc)

    status_by_gpu: dict[str, dict[str, Any]] = {}
    for row in gpu_status or []:
        if not isinstance(row, dict):
            continue
        status_by_gpu[str(row.get("index"))] = row

    external_blocked: set[str] = set()
    active_untracked: set[str] = set()
    reasons: dict[str, list[str]] = {}

    def add_reason(index: str, reason: str) -> None:
        reasons.setdefault(index, []).append(reason)

    threshold = max(0, int(foreign_gpu_memory_threshold_mb))
    for index in managed_gpu_indices:
        untracked_rows = [row for row in proc_by_gpu.get(index, []) if not row.get("controller_job_id")]
        if index in active_gpu:
            if untracked_rows:
                active_untracked.add(index)
                for row in untracked_rows:
                    add_reason(
                        index,
                        f"active_gpu_untracked_proc pid={row.get('pid')} name={row.get('process_name')} mem={row.get('used_memory_mb')}",
                    )
            continue

        blocked = False
        for row in untracked_rows:
            mem_used = row.get("used_memory_mb")
            if mem_used is None:
                blocked = True
            else:
                try:
                    blocked = int(mem_used) >= threshold
                except Exception:
                    blocked = True
            if blocked:
                add_reason(
                    index,
                    f"foreign_proc pid={row.get('pid')} name={row.get('process_name')} mem={row.get('used_memory_mb')}",
                )
                break

        if not blocked:
            status_row = status_by_gpu.get(index)
            mem_used = None if status_row is None else status_row.get("mem_used_mb")
            try:
                if mem_used is not None and int(mem_used) >= threshold:
                    blocked = True
                    add_reason(index, f"foreign_mem_only mem={mem_used}")
            except Exception:
                pass

        if blocked:
            external_blocked.add(index)

    schedulable = [idx for idx in managed_gpu_indices if idx not in external_blocked]
    unavailable = sorted(active_gpu | external_blocked, key=int)
    free = [idx for idx in schedulable if idx not in active_gpu]
    return {
        "active_gpu_indices": sorted(active_gpu, key=int),
        "active_untracked_gpu_indices": sorted(active_untracked, key=int),
        "externally_blocked_gpu_indices": sorted(external_blocked, key=int),
        "schedulable_gpu_indices": schedulable,
        "unavailable_gpu_indices": unavailable,
        "free_gpu_indices": free,
        "gpu_external_block_reasons": reasons,
    }


def count_json_files(path: Path) -> int:
    return sum(1 for child in path.iterdir() if child.is_file() and child.suffix == ".json")


def runtime_counts(layout: RuntimeLayout) -> dict[str, int]:
    return {
        "queue": count_runtime_specs(layout.queue_dir),
        "running_specs": count_runtime_specs(layout.running_dir),
        "running_meta": sum(1 for child in layout.running_dir.iterdir() if child.is_file() and child.name.endswith(".meta.json")),
        "done": count_runtime_results(layout.done_dir),
        "failed": count_runtime_results(layout.failed_dir),
        "cancelled": count_runtime_results(layout.cancelled_dir),
        "kill": count_runtime_specs(layout.kill_dir),
        "control_queue": count_runtime_specs(layout.control_queue_dir),
        "control_done": count_runtime_results(layout.control_done_dir),
        "control_failed": count_runtime_results(layout.control_failed_dir),
    }


def reserve_job(
    request: JobRequest,
    active_runs: list[ActiveRun],
    managed_gpu_indices: list[str],
    *,
    schedulable_gpu_indices: list[str] | None = None,
) -> list[str] | None:
    if any(run.exclusive for run in active_runs):
        return None
    if request.exclusive and active_runs:
        return None

    occupied = set(active_gpu_indices(active_runs))
    schedulable = normalize_gpu_indices(schedulable_gpu_indices or managed_gpu_indices)
    if request.requested_gpu_indices:
        if managed_gpu_indices:
            unknown = sorted(set(request.requested_gpu_indices) - set(managed_gpu_indices), key=int)
            if unknown:
                raise ValueError(f"requested_gpu_indices {unknown} outside managed_gpus {managed_gpu_indices}")
        if occupied.intersection(request.requested_gpu_indices):
            return None
        if any(idx not in schedulable for idx in request.requested_gpu_indices):
            return None
        return request.requested_gpu_indices

    if request.gpu_count > 0:
        candidate_pool = request.allowed_gpu_indices or schedulable
        if not candidate_pool:
            raise ValueError("gpu_count requires allow_gpu_indices or controller managed_gpus")
        free = [idx for idx in candidate_pool if idx in set(schedulable) and idx not in occupied]
        if len(free) < request.gpu_count:
            return None
        return free[: request.gpu_count]

    return []


def fail_queued_job(layout: RuntimeLayout, spec_path: Path, *, reason: str) -> None:
    payload, payload_error = read_json_safe(spec_path)
    failure_reason = reason if payload_error is None else f"{reason}; payload_error={payload_error}"
    job_id = str((payload or {}).get("job_id") or spec_path.stem)
    move_invalid_item(
        spec_path,
        out_dir=layout.failed_dir,
        item_id=job_id,
        payload=payload,
        status="failed",
        reason=failure_reason,
        extra={"elapsed_seconds": 0.0, "returncode": None},
    )


def fail_running_job(
    layout: RuntimeLayout,
    *,
    running_spec_path: Path,
    running_meta_path: Path | None,
    payload: dict[str, Any] | None,
    job_id: str,
    reason: str,
    extra: dict[str, Any] | None = None,
) -> None:
    move_invalid_item(
        running_spec_path,
        out_dir=layout.failed_dir,
        item_id=job_id,
        payload=payload,
        status="failed",
        reason=reason,
        extra=extra,
    )
    if running_meta_path is not None and running_meta_path.exists():
        shutil.move(str(running_meta_path), str(layout.failed_dir / running_meta_path.name))


def refresh_run_meta_from_disk(run: ActiveRun) -> dict[str, Any]:
    payload, error = read_json_safe(run.running_meta_path)
    if error is None and payload is not None:
        run.meta = payload
    return run.meta


def launch_job(
    layout: RuntimeLayout,
    spec_path: Path,
    request: JobRequest,
    allocated_gpu_indices: list[str],
    *,
    supervisor_grace_seconds: float,
) -> ActiveRun:
    running_path = layout.running_dir / spec_path.name
    payload = dict(request.payload)
    shutil.move(str(spec_path), str(running_path))

    workdir = Path(request.workdir or layout.root).resolve()
    log_path = layout.jobs_log_dir / f"{request.job_id}.log"
    event_path = layout.jobs_log_dir / f"{request.job_id}.events.jsonl"
    meta_path = layout.running_dir / f"{request.job_id}.meta.json"

    meta = {
        "schema_version": SCHEMA_VERSION,
        "job_id": request.job_id,
        "command": list(request.command),
        "workdir": str(workdir),
        "timeout_seconds": request.timeout_seconds,
        "queued_from_path": str(spec_path),
        "started_at_epoch": time.time(),
        "log_path": str(log_path),
        "event_log_path": str(event_path),
        "running_spec_path": str(running_path),
        "allocated_gpu_indices": allocated_gpu_indices,
        "exclusive": request.exclusive,
        "gpu_count": request.gpu_count,
        "allowed_gpu_indices": request.allowed_gpu_indices,
        "requested_gpu_indices": request.requested_gpu_indices,
        "reservation_source": request.reservation_source,
        "set_cuda_visible_devices": request.set_cuda_visible_devices,
        "supervisor_state": "launching",
        "controller_launch_host": socket.gethostname(),
        "controller_launch_pid": os.getpid(),
    }
    write_json(meta_path, meta)

    try:
        spawned = subprocess.Popen(
            [
                sys.executable,
                str(SUPERVISOR_SCRIPT),
                "--spec",
                str(running_path),
                "--meta",
                str(meta_path),
                "--log-path",
                str(log_path),
                "--event-path",
                str(event_path),
                "--grace-seconds",
                str(supervisor_grace_seconds),
            ],
            cwd=str(layout.root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        proc = ManagedProcess.from_popen(spawned)
        meta.update(
            {
                "supervisor_pid": proc.pid,
                "supervisor_pgid": proc.pgid,
                "supervisor_start_time_ticks": proc.start_time_ticks,
                "supervisor_state": "spawned",
            }
        )
        write_json(meta_path, meta)
    except Exception as exc:
        fail_running_job(
            layout,
            running_spec_path=running_path,
            running_meta_path=meta_path if meta_path.exists() else None,
            payload=payload,
            job_id=request.job_id,
            reason=f"launch_failed: {type(exc).__name__}: {exc}",
            extra={"elapsed_seconds": 0.0, "returncode": None},
        )
        raise
    return ActiveRun(
        meta=meta,
        proc=proc,
        running_spec_path=running_path,
        running_meta_path=meta_path,
        allocated_gpu_indices=allocated_gpu_indices,
        exclusive=request.exclusive,
        job_log_path=log_path,
        job_event_path=event_path,
    )


def summarize_active_run(run: ActiveRun) -> dict[str, Any]:
    return {
        **run.meta,
        "pid": run.proc.pid,
        "pgid": run.proc.pgid,
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
    if run.log_handle is not None and not run.log_handle.closed:
        run.log_handle.close()

    meta = refresh_run_meta_from_disk(run)
    returncode = run.proc.poll()
    finished_at_epoch = time.time()
    elapsed = finished_at_epoch - float(meta.get("started_at_epoch", finished_at_epoch))
    detached_unknown_exit = run.proc.popen is None and returncode == -1 and "final_returncode" not in meta
    result_status = status or str(meta.get("final_status") or "")
    if not result_status:
        if detached_unknown_exit:
            result_status = "done"
        else:
            result_status = "done" if returncode == 0 else "failed"
    effective_returncode = meta.get("final_returncode")
    if effective_returncode is None and not detached_unknown_exit:
        effective_returncode = returncode
    result = {
        **meta,
        "finished_at_epoch": finished_at_epoch,
        "elapsed_seconds": elapsed,
        "returncode": None if detached_unknown_exit else effective_returncode,
        "status": result_status,
        "schema_version": SCHEMA_VERSION,
    }
    if detached_unknown_exit:
        result["returncode_source"] = "detached_unknown"
    if extra:
        result.update(extra)
    if target_dir is not None:
        out_dir = target_dir
    elif result_status == "done":
        out_dir = layout.done_dir
    elif result_status == "cancelled":
        out_dir = layout.cancelled_dir
    else:
        out_dir = layout.failed_dir
    target_spec = out_dir / run.running_spec_path.name
    if run.running_spec_path.exists():
        shutil.move(str(run.running_spec_path), str(target_spec))
    if run.running_meta_path.exists():
        shutil.move(str(run.running_meta_path), str(out_dir / run.running_meta_path.name))
    write_json(out_dir / f"{meta['job_id']}.result.json", result)
    return result


def controller_payload(
    layout: RuntimeLayout,
    active_runs: list[ActiveRun],
    *,
    managed_gpu_indices: list[str],
    gpu_availability: dict[str, Any] | None = None,
    gpu_status: list[dict[str, Any]] | None = None,
    gpu_processes: list[dict[str, Any]] | None = None,
    stop_when_idle: bool = False,
    last_control_action: str | None = None,
    acquired_at_epoch: float | None = None,
) -> dict[str, Any]:
    active_jobs = []
    for run in active_runs:
        refresh_run_meta_from_disk(run)
        active_jobs.append(summarize_active_run(run))
    active_job = active_jobs[0] if len(active_jobs) == 1 else None
    gpu_availability = gpu_availability or summarize_gpu_availability(
        managed_gpu_indices,
        active_runs,
        gpu_status=gpu_status,
        gpu_processes=gpu_processes,
    )
    active_gpu = gpu_availability["active_gpu_indices"]
    counts = runtime_counts(layout)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at_epoch": time.time(),
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "runtime_root": str(layout.root),
        "runtime_name": layout.root.name,
        "shared_root": str(layout.shared_root),
        "controller_state_path": str(layout.controller_state),
        "controller_lease_path": str(layout.controller_lease_path),
        "controller_heartbeat_path": str(layout.controller_heartbeat),
        "controller_events_path": str(layout.controller_events),
        "controller_log_path": str(layout.controller_log),
        "controller_admin_audit_path": str(layout.controller_admin_audit),
        "shared_controller_state_path": str(layout.shared_controller_state),
        "shared_controller_lease_path": str(layout.shared_controller_lease_path),
        "active_job": active_job,
        "active_jobs": active_jobs,
        "active_job_count": len(active_jobs),
        "queue_count": counts["queue"],
        "running_count": len(active_jobs),
        "done_count": counts["done"],
        "failed_count": counts["failed"],
        "cancelled_count": counts["cancelled"],
        "kill_count": counts["kill"],
        "control_queue_count": counts["control_queue"],
        "control_done_count": counts["control_done"],
        "control_failed_count": counts["control_failed"],
        "managed_gpu_indices": managed_gpu_indices,
        "controller_gpu_indices": managed_gpu_indices,
        "active_gpu_indices": active_gpu,
        "active_untracked_gpu_indices": gpu_availability["active_untracked_gpu_indices"],
        "externally_blocked_gpu_indices": gpu_availability["externally_blocked_gpu_indices"],
        "schedulable_gpu_indices": gpu_availability["schedulable_gpu_indices"],
        "unavailable_gpu_indices": gpu_availability["unavailable_gpu_indices"],
        "occupied_gpu_indices": gpu_availability["unavailable_gpu_indices"],
        "free_gpu_indices": gpu_availability["free_gpu_indices"],
        "gpu_external_block_reasons": gpu_availability["gpu_external_block_reasons"],
        "gpu_status": gpu_status or [],
        "gpu_processes": gpu_processes or [],
        "counts": counts,
        "stop_when_idle": stop_when_idle,
    }
    if acquired_at_epoch is not None:
        payload["controller_started_at_epoch"] = acquired_at_epoch
    if last_control_action:
        payload["last_control_action"] = last_control_action
    return payload


def terminate_process_group(proc: ManagedProcess, *, signal_name: str, grace_seconds: float) -> int | None:
    if proc.poll() is not None:
        return proc.poll()
    sig = signal_from_name(signal_name)
    try:
        os.killpg(proc.pgid, sig)
    except Exception:
        if process_exists(proc.pid):
            os.kill(proc.pid, sig)
    deadline = time.time() + max(0.0, grace_seconds)
    while time.time() < deadline:
        rc = proc.poll()
        if rc is not None:
            return rc
        time.sleep(0.2)
    if proc.poll() is None:
        force_kill = getattr(signal, "SIGKILL", signal.SIGTERM)
        try:
            os.killpg(proc.pgid, force_kill)
        except Exception:
            if process_exists(proc.pid):
                os.kill(proc.pid, force_kill)
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


def move_job_to_cancelled(
    layout: RuntimeLayout,
    spec_path: Path,
    *,
    reason: str,
    request_id: str,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec, payload_error = read_json_safe(spec_path)
    if payload_error is not None:
        spec = {}
    job_id = str((spec or {}).get("job_id") or spec_path.stem)
    target_spec = layout.cancelled_dir / spec_path.name
    if spec_path.exists():
        shutil.move(str(spec_path), str(target_spec))
    result = {
        **(spec or {}),
        "schema_version": SCHEMA_VERSION,
        "finished_at_epoch": time.time(),
        "status": "cancelled",
        "cancel_reason": reason,
        "control_request_id": request_id,
    }
    if request:
        result["control_action"] = request.get("action")
        result["requested_by"] = request.get("requested_by")
        result["requested_from_host"] = request.get("requested_from_host")
        result["requested_from_pid"] = request.get("requested_from_pid")
        result["request_source"] = request.get("request_source")
    if payload_error is not None:
        result["payload_error"] = payload_error
    write_json(layout.cancelled_dir / f"{job_id}.result.json", result)
    return result


def purge_queue(
    layout: RuntimeLayout,
    *,
    reason: str,
    request_id: str,
    job_ids: list[str] | None = None,
    request: dict[str, Any] | None = None,
) -> list[str]:
    requested = set(job_ids or [])
    purged: list[str] = []
    for spec_path in queued_specs(layout):
        spec, payload_error = read_json_safe(spec_path)
        if payload_error is not None:
            fail_queued_job(layout, spec_path, reason=f"purge_queue rejected invalid payload: {payload_error}")
            continue
        assert spec is not None
        job_id = str(spec.get("job_id") or spec_path.stem)
        if requested and job_id not in requested:
            continue
        move_job_to_cancelled(layout, spec_path, reason=reason, request_id=request_id, request=request)
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
        "schema_version": SCHEMA_VERSION,
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
    control_action: str = "cancel_active_job",
    target_dir: Path | None = None,
    request: dict[str, Any] | None = None,
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
                target_dir=target_dir or layout.cancelled_dir,
                status="cancelled",
                extra={
                    "control_request_id": request_id,
                    "stop_reason": reason,
                    "stopped_by_signal": signal_name.upper(),
                    "control_action": control_action,
                    "returncode": returncode,
                    "requested_by": None if request is None else request.get("requested_by"),
                    "requested_from_host": None if request is None else request.get("requested_from_host"),
                    "requested_from_pid": None if request is None else request.get("requested_from_pid"),
                    "request_source": None if request is None else request.get("request_source"),
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
    request, request_error = read_json_safe(request_path)
    if request_error is not None:
        request_id = request_path.stem
        write_control_result(
            layout,
            request_path,
            {"request_id": request_id, "source_path": str(request_path)},
            status="failed",
            details={"message": f"invalid control request: {request_error}"},
        )
        return active_runs, stop_when_idle, False, "invalid_control_request"

    assert request is not None
    action = str(request.get("action") or "").strip().lower()
    request_id = str(request.get("request_id") or request_path.stem)
    reason = str(request.get("reason") or action or request_id)
    signal_name = str(request.get("signal") or "TERM")
    grace_seconds = float(request.get("grace_seconds", 15.0) or 15.0)
    job_ids = string_list(request.get("job_ids"))
    last_control_action = action or "invalid"
    should_exit = False
    append_controller_event(
        layout,
        "control_request_received",
        audit=True,
        message=f"action={action or 'invalid'} request_id={request_id}",
        request_id=request_id,
        action=action,
        reason=reason,
        signal=signal_name,
        grace_seconds=grace_seconds,
        job_ids=job_ids,
        requested_by=request.get("requested_by"),
        requested_from_host=request.get("requested_from_host"),
        requested_from_pid=request.get("requested_from_pid"),
        request_source=request.get("request_source"),
    )

    if action == "cancel_active_job":
        active_runs, cancelled = cancel_matching_active_runs(
            layout,
            active_runs,
            request_id=request_id,
            reason=reason,
            signal_name=signal_name,
            grace_seconds=grace_seconds,
            job_ids=job_ids,
            request=request,
        )
        write_control_result(
            layout,
            request_path,
            request,
            status="done" if cancelled else "noop",
            details={"cancelled_job_ids": cancelled, "cancelled_job_count": len(cancelled)},
        )
        append_controller_event(
            layout,
            "control_request_completed",
            audit=True,
            message=f"action={action} request_id={request_id}",
            request_id=request_id,
            action=action,
            cancelled_job_ids=cancelled,
            cancelled_job_count=len(cancelled),
        )
        return active_runs, stop_when_idle, should_exit, last_control_action

    if action == "purge_queue":
        purged = purge_queue(layout, reason=reason, request_id=request_id, job_ids=job_ids, request=request)
        write_control_result(
            layout,
            request_path,
            request,
            status="done",
            details={"purged_job_ids": purged, "purged_job_count": len(purged)},
        )
        append_controller_event(
            layout,
            "control_request_completed",
            audit=True,
            message=f"action={action} request_id={request_id}",
            request_id=request_id,
            action=action,
            purged_job_ids=purged,
            purged_job_count=len(purged),
        )
        return active_runs, stop_when_idle, should_exit, last_control_action

    if action in {"stop_controller", "drain_and_stop", "retire_controller"}:
        purge_before_stop = bool_value(request.get("purge_queue"), default=action == "retire_controller")
        cancel_before_stop = bool_value(request.get("cancel_active_job"), default=action == "retire_controller")
        details: dict[str, Any] = {}
        if purge_before_stop:
            purged = purge_queue(layout, reason=reason, request_id=request_id, job_ids=job_ids, request=request)
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
                request=request,
            )
            details["cancelled_job_ids"] = cancelled
            details["cancelled_job_count"] = len(cancelled)
        stop_when_idle = True
        should_exit = not active_runs
        details["stop_when_idle"] = stop_when_idle
        details["controller_will_exit_now"] = should_exit
        write_control_result(layout, request_path, request, status="accepted", details=details)
        audit_details = dict(details)
        audit_details.setdefault("stop_when_idle", stop_when_idle)
        audit_details.setdefault("controller_will_exit_now", should_exit)
        append_controller_event(
            layout,
            "control_request_completed",
            audit=True,
            message=f"action={action} request_id={request_id}",
            request_id=request_id,
            action=action,
            **audit_details,
        )
        return active_runs, stop_when_idle, should_exit, last_control_action

    write_control_result(
        layout,
        request_path,
        request,
        status="failed",
        details={"message": f"unsupported action: {action!r}"},
    )
    append_controller_event(
        layout,
        "control_request_failed",
        audit=True,
        message=f"unsupported action={action!r} request_id={request_id}",
        request_id=request_id,
        action=action,
    )
    return active_runs, stop_when_idle, should_exit, last_control_action


def adopt_running_jobs(layout: RuntimeLayout) -> list[ActiveRun]:
    adopted: list[ActiveRun] = []
    spec_map = {path.stem: path for path in layout.running_dir.glob("*.json") if not path.name.endswith(".meta.json")}
    meta_map = {
        path.name[: -len(".meta.json")]: path
        for path in layout.running_dir.glob("*.meta.json")
    }
    for job_id in sorted(set(spec_map) | set(meta_map)):
        running_spec_path = spec_map.get(job_id, layout.running_dir / f"{job_id}.json")
        running_meta_path = layout.running_dir / f"{job_id}.meta.json"
        spec_payload, spec_error = read_json_safe(running_spec_path) if running_spec_path.exists() else (None, "missing spec")
        meta_payload, meta_error = read_json_safe(running_meta_path) if running_meta_path.exists() else (None, "missing meta")
        if meta_error is not None:
            fail_running_job(
                layout,
                running_spec_path=running_spec_path,
                running_meta_path=running_meta_path if running_meta_path.exists() else None,
                payload=spec_payload,
                job_id=str((spec_payload or {}).get("job_id") or job_id),
                reason=f"bootstrap_reconcile_failed: {meta_error}",
                extra={"elapsed_seconds": 0.0, "returncode": None},
            )
            continue
        assert meta_payload is not None
        final_status = str(meta_payload.get("final_status") or "")
        if final_status:
            run = ActiveRun(
                meta=meta_payload,
                proc=ManagedProcess.attach(-1, -1),
                running_spec_path=running_spec_path,
                running_meta_path=running_meta_path,
                allocated_gpu_indices=normalize_gpu_indices(meta_payload.get("allocated_gpu_indices")),
                exclusive=bool(meta_payload.get("exclusive")),
                job_log_path=Path(str(meta_payload.get("log_path"))) if meta_payload.get("log_path") else None,
                job_event_path=Path(str(meta_payload.get("event_log_path"))) if meta_payload.get("event_log_path") else None,
                attached=True,
            )
            finalize_job(layout, run, status=final_status)
            append_controller_event(
                layout,
                "job_reconciled_after_restart",
                message=f"finalized completed supervisor metadata for {meta_payload.get('job_id')}",
                job_id=meta_payload.get("job_id"),
                final_status=final_status,
            )
            continue
        pid = int(meta_payload.get("supervisor_pid") or 0)
        pgid = int(meta_payload.get("supervisor_pgid") or pid or 0)
        start_time_ticks = meta_payload.get("supervisor_start_time_ticks")
        command = [str(part) for part in meta_payload.get("resolved_command") or []]
        if pid <= 0 or not process_exists(pid) or not pid_matches_command(pid, command):
            fail_running_job(
                layout,
                running_spec_path=running_spec_path,
                running_meta_path=running_meta_path,
                payload=spec_payload,
                job_id=str(meta_payload.get("job_id") or job_id),
                reason="bootstrap_reconcile_failed: tracked supervisor pid is not alive or no longer matches expected command",
                extra={"elapsed_seconds": 0.0, "returncode": None},
            )
            continue
        allocated_gpu_indices = normalize_gpu_indices(meta_payload.get("allocated_gpu_indices"))
        adopted.append(
            ActiveRun(
                meta=meta_payload,
                proc=ManagedProcess.attach(pid, pgid, start_time_ticks=int(start_time_ticks) if start_time_ticks is not None else None),
                running_spec_path=running_spec_path,
                running_meta_path=running_meta_path,
                allocated_gpu_indices=allocated_gpu_indices,
                exclusive=bool(meta_payload.get("exclusive")),
                job_log_path=Path(str(meta_payload.get("log_path"))) if meta_payload.get("log_path") else None,
                job_event_path=Path(str(meta_payload.get("event_log_path"))) if meta_payload.get("event_log_path") else None,
                attached=True,
            )
        )
    return adopted


def queued_kill_requests(layout: RuntimeLayout) -> list[Path]:
    return sorted(path for path in layout.kill_dir.iterdir() if path.is_file())


def process_kill_requests(
    layout: RuntimeLayout,
    active_runs: list[ActiveRun],
    *,
    enabled: bool,
) -> tuple[list[ActiveRun], bool]:
    handled_any = False
    if not enabled:
        return active_runs, handled_any
    for request_path in queued_kill_requests(layout):
        job_id = request_path.stem.strip()
        request_id = f"kill_signal_{job_id or request_path.name}_{int(time.time())}"
        reason = "jobs/kill compatibility signal"
        request_payload = {
            "request_id": request_id,
            "action": "compat_kill_signal",
            "reason": reason,
            "job_ids": [job_id] if job_id else [],
            "requested_by": "unknown_compat_writer",
            "requested_from_host": "unknown",
            "request_source": f"legacy:{request_path}",
        }
        append_controller_event(
            layout,
            "compat_kill_request_detected",
            audit=True,
            message=f"legacy jobs/kill request for {job_id or request_path.name}",
            request_id=request_id,
            job_id=job_id,
            request_source=str(request_path),
        )
        purged = purge_queue(
            layout,
            reason=reason,
            request_id=request_id,
            job_ids=[job_id] if job_id else None,
            request=request_payload,
        )
        active_runs, cancelled = cancel_matching_active_runs(
            layout,
            active_runs,
            request_id=request_id,
            reason=reason,
            signal_name="TERM",
            grace_seconds=15.0,
            job_ids=[job_id] if job_id else [],
            control_action="kill_signal",
            request=request_payload,
        )
        if purged or cancelled:
            handled_any = True
            append_controller_event(
                layout,
                "job_killed",
                audit=True,
                message=f"legacy kill handled for {job_id}",
                job_id=job_id,
                purged_job_ids=purged,
                cancelled_job_ids=cancelled,
            )
        request_path.unlink(missing_ok=True)
    return active_runs, handled_any


def controller_lease_payload(
    layout: RuntimeLayout,
    *,
    heartbeat_seconds: float,
    acquired_at_epoch: float,
    released: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "runtime_root": str(layout.root),
        "runtime_name": layout.root.name,
        "shared_root": str(layout.shared_root),
        "acquired_at_epoch": acquired_at_epoch,
        "updated_at_epoch": time.time(),
        "heartbeat_seconds": heartbeat_seconds,
        "released": released,
    }


def lease_is_stale(payload: dict[str, Any], *, heartbeat_seconds: float) -> bool:
    updated = float(payload.get("updated_at_epoch") or 0.0)
    pid = int(payload.get("pid") or 0)
    stale_after = max(5.0, heartbeat_seconds * 3.0)
    if updated <= 0:
        return True
    if time.time() - updated > stale_after:
        return True
    return not process_exists(pid)


def acquire_controller_lease(layout: RuntimeLayout, *, heartbeat_seconds: float) -> float:
    acquired_at_epoch = time.time()
    while True:
        try:
            layout.controller_lock_dir.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            payload = None
            error = None
            for _ in range(10):
                payload, error = read_json_safe(layout.controller_lease_path)
                if error is None and payload is not None:
                    break
                time.sleep(0.1)
            if error is None and payload is not None:
                for _ in range(10):
                    if lease_is_stale(payload, heartbeat_seconds=heartbeat_seconds):
                        break
                    time.sleep(0.1)
                    payload, error = read_json_safe(layout.controller_lease_path)
                    if error is not None or payload is None:
                        break
                if error is None and payload is not None and not lease_is_stale(payload, heartbeat_seconds=heartbeat_seconds):
                    raise RuntimeError(
                        f"controller lease already held by pid={payload.get('pid')} host={payload.get('hostname')}"
                    )
            lock_age_seconds = 0.0
            try:
                lock_age_seconds = max(0.0, time.time() - layout.controller_lock_dir.stat().st_mtime)
            except Exception:
                lock_age_seconds = 0.0
            if payload is None or error is not None:
                if lock_age_seconds <= max(1.0, heartbeat_seconds * 3.0):
                    raise RuntimeError("controller lease lock exists but lease payload is not readable yet")
            shutil.rmtree(layout.controller_lock_dir, ignore_errors=True)
            time.sleep(0.1)
    write_json(
        layout.controller_lease_path,
        controller_lease_payload(layout, heartbeat_seconds=heartbeat_seconds, acquired_at_epoch=acquired_at_epoch),
    )
    write_json(
        layout.shared_controller_lease_path,
        controller_lease_payload(layout, heartbeat_seconds=heartbeat_seconds, acquired_at_epoch=acquired_at_epoch),
    )
    return acquired_at_epoch


def refresh_controller_lease(layout: RuntimeLayout, *, heartbeat_seconds: float, acquired_at_epoch: float) -> None:
    write_json(
        layout.controller_lease_path,
        controller_lease_payload(layout, heartbeat_seconds=heartbeat_seconds, acquired_at_epoch=acquired_at_epoch),
    )
    write_json(
        layout.shared_controller_lease_path,
        controller_lease_payload(layout, heartbeat_seconds=heartbeat_seconds, acquired_at_epoch=acquired_at_epoch),
    )


def release_controller_lease(layout: RuntimeLayout, *, heartbeat_seconds: float, acquired_at_epoch: float) -> None:
    write_json(
        layout.controller_lease_path,
        controller_lease_payload(
            layout,
            heartbeat_seconds=heartbeat_seconds,
            acquired_at_epoch=acquired_at_epoch,
            released=True,
        ),
    )
    write_json(
        layout.shared_controller_lease_path,
        controller_lease_payload(
            layout,
            heartbeat_seconds=heartbeat_seconds,
            acquired_at_epoch=acquired_at_epoch,
            released=True,
        ),
    )
    shutil.rmtree(layout.controller_lock_dir, ignore_errors=True)


def harvest_finished_runs(layout: RuntimeLayout, active_runs: list[ActiveRun]) -> list[ActiveRun]:
    survivors: list[ActiveRun] = []
    for run in active_runs:
        handle_timeout(run)
        rc = run.proc.poll()
        if rc is None:
            survivors.append(run)
            continue
        result = finalize_job(layout, run)
        append_controller_event(
            layout,
            "job_finished",
            message=f"job_id={result.get('job_id')} status={result.get('status')}",
            job_id=result.get("job_id"),
            status=result.get("status"),
            returncode=result.get("returncode"),
            allocated_gpu_indices=result.get("allocated_gpu_indices"),
        )
        append_jsonl(
            layout.controller_heartbeat,
            {
                "event": "job_finished",
                "job_id": result.get("job_id"),
                "returncode": result.get("returncode", rc),
                "allocated_gpu_indices": result.get("allocated_gpu_indices", run.allocated_gpu_indices),
                "status": result.get("status"),
                "updated_at_epoch": time.time(),
            },
        )
    return survivors


def dispatch_launchable_jobs(
    layout: RuntimeLayout,
    active_runs: list[ActiveRun],
    managed_gpu_indices: list[str],
    *,
    schedulable_gpu_indices: list[str],
    supervisor_grace_seconds: float,
    max_new_jobs: int | None = None,
) -> list[ActiveRun]:
    launched = 0
    for spec_path in queued_specs(layout):
        if max_new_jobs is not None and launched >= max_new_jobs:
            break
        try:
            payload, payload_error = read_json_safe(spec_path)
            if payload_error is not None:
                raise ValueError(f"invalid queue payload: {payload_error}")
            assert payload is not None
            request = load_job_request_payload(payload, source_path=spec_path)
            allocated = reserve_job(
                request,
                active_runs,
                managed_gpu_indices,
                schedulable_gpu_indices=schedulable_gpu_indices,
            )
        except ValueError as exc:
            fail_queued_job(layout, spec_path, reason=str(exc))
            append_controller_event(
                layout,
                "job_rejected",
                message=f"spec={spec_path.name} reason={exc}",
                spec_path=str(spec_path),
                reason=str(exc),
            )
            continue
        if allocated is None:
            continue
        try:
            append_controller_event(
                layout,
                "job_reserved",
                message=f"job_id={request.job_id}",
                job_id=request.job_id,
                allocated_gpu_indices=allocated,
                reservation_source=request.reservation_source,
            )
            run = launch_job(
                layout,
                spec_path,
                request,
                allocated,
                supervisor_grace_seconds=supervisor_grace_seconds,
            )
        except Exception as exc:
            append_controller_event(
                layout,
                "job_launch_failed",
                message=f"job_id={request.job_id} reason={type(exc).__name__}: {exc}",
                job_id=request.job_id,
                spec_path=str(spec_path),
                reason=f"{type(exc).__name__}: {exc}",
            )
            continue
        active_runs.append(run)
        launched += 1
        append_controller_event(
            layout,
            "job_started",
            message=f"job_id={run.meta['job_id']}",
            job_id=run.meta["job_id"],
            supervisor_pid=run.proc.pid,
            allocated_gpu_indices=allocated,
            reservation_source=run.meta["reservation_source"],
        )
        append_jsonl(
            layout.controller_heartbeat,
            {
                "event": "job_started",
                "job_id": run.meta["job_id"],
                "pid": int(run.meta.get("child_pid") or run.proc.pid),
                "supervisor_pid": run.proc.pid,
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
    acquired_at_epoch = acquire_controller_lease(layout, heartbeat_seconds=args.heartbeat_seconds)
    append_controller_event(
        layout,
        "controller_started",
        message="controller lease acquired",
        managed_gpu_override=args.managed_gpu_indices,
        compat_kill_queue_enabled=args.enable_compat_kill_queue,
        supervisor_grace_seconds=args.supervisor_grace_seconds,
    )
    managed_gpu_indices = detect_managed_gpu_indices(args.managed_gpu_indices)
    active_runs = adopt_running_jobs(layout)
    for run in active_runs:
        append_controller_event(
            layout,
            "job_reattached",
            message=f"job_id={run.meta.get('job_id')}",
            job_id=run.meta.get("job_id"),
            supervisor_pid=run.proc.pid,
            allocated_gpu_indices=run.allocated_gpu_indices,
        )
        append_jsonl(
            layout.controller_heartbeat,
            {
                "event": "job_reattached",
                "job_id": run.meta.get("job_id"),
                "pid": run.proc.pid,
                "allocated_gpu_indices": run.allocated_gpu_indices,
                "updated_at_epoch": time.time(),
            },
        )
    startup_gpu_status = probe_gpu_status(managed_gpu_indices)
    startup_gpu_processes = probe_gpu_processes(managed_gpu_indices, active_runs)
    startup_gpu_availability = summarize_gpu_availability(
        managed_gpu_indices,
        active_runs,
        gpu_status=startup_gpu_status,
        gpu_processes=startup_gpu_processes,
        foreign_gpu_memory_threshold_mb=args.foreign_gpu_memory_threshold_mb,
    )
    if not active_runs and args.startup_min_schedulable_gpu_count > 0:
        schedulable_count = len(startup_gpu_availability["schedulable_gpu_indices"])
        if schedulable_count < args.startup_min_schedulable_gpu_count:
            payload = controller_payload(
                layout,
                active_runs,
                managed_gpu_indices=managed_gpu_indices,
                gpu_availability=startup_gpu_availability,
                gpu_status=startup_gpu_status,
                gpu_processes=startup_gpu_processes,
                stop_when_idle=False,
                last_control_action="startup_unhealthy",
                acquired_at_epoch=acquired_at_epoch,
            )
            payload["startup_health_status"] = "failed"
            payload["startup_health_reason"] = (
                f"schedulable_gpu_count={schedulable_count} < required={args.startup_min_schedulable_gpu_count}"
            )
            write_controller_state_snapshots(layout, payload, acquired_at_epoch=acquired_at_epoch)
            append_controller_event(
                layout,
                "controller_startup_unhealthy",
                audit=True,
                message=payload["startup_health_reason"],
                schedulable_gpu_indices=startup_gpu_availability["schedulable_gpu_indices"],
                externally_blocked_gpu_indices=startup_gpu_availability["externally_blocked_gpu_indices"],
            )
            append_jsonl(
                layout.controller_heartbeat,
                {
                    **payload,
                    "event": "controller_startup_unhealthy",
                },
            )
            return 2
    last_heartbeat = 0.0
    stop_when_idle = False
    last_control_action: str | None = None
    launched_any = False
    shutdown_reason: str | None = None
    signal_count = 0
    exception_backoff_seconds = 1.0

    def request_shutdown(signum: int, _frame: Any) -> None:
        nonlocal stop_when_idle, shutdown_reason, signal_count, last_control_action
        signal_count += 1
        stop_when_idle = True
        shutdown_reason = f"signal:{signal.Signals(signum).name}"
        last_control_action = shutdown_reason
        append_controller_event(
            layout,
            "shutdown_requested",
            audit=True,
            message=shutdown_reason,
            reason=shutdown_reason,
            signal_count=signal_count,
        )
        append_jsonl(
            layout.controller_heartbeat,
            {
                "event": "shutdown_requested",
                "reason": shutdown_reason,
                "signal_count": signal_count,
                "updated_at_epoch": time.time(),
                "pid": os.getpid(),
            },
        )
        if signal_count >= 2:
            raise KeyboardInterrupt(shutdown_reason)

    previous_sigint = signal.signal(signal.SIGINT, request_shutdown)
    previous_sigterm = signal.signal(signal.SIGTERM, request_shutdown)

    try:
        while True:
            try:
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
                        gpu_status = probe_gpu_status(managed_gpu_indices)
                        gpu_processes = probe_gpu_processes(managed_gpu_indices, active_runs)
                        gpu_availability = summarize_gpu_availability(
                            managed_gpu_indices,
                            active_runs,
                            gpu_status=gpu_status,
                            gpu_processes=gpu_processes,
                            foreign_gpu_memory_threshold_mb=args.foreign_gpu_memory_threshold_mb,
                        )
                        payload = controller_payload(
                            layout,
                            active_runs,
                            managed_gpu_indices=managed_gpu_indices,
                            gpu_availability=gpu_availability,
                            gpu_status=gpu_status,
                            gpu_processes=gpu_processes,
                            stop_when_idle=stop_when_idle,
                            last_control_action=last_control_action,
                            acquired_at_epoch=acquired_at_epoch,
                        )
                        write_controller_state_snapshots(layout, payload, acquired_at_epoch=acquired_at_epoch)
                        refresh_controller_lease(
                            layout,
                            heartbeat_seconds=args.heartbeat_seconds,
                            acquired_at_epoch=acquired_at_epoch,
                        )
                        append_controller_event(
                            layout,
                            "controller_stopped",
                            audit=True,
                            message="stopped by control request",
                            reason="control_request",
                        )
                        append_jsonl(
                            layout.controller_heartbeat,
                            {
                                **payload,
                                "event": "controller_stopped",
                                "reason": "control_request",
                            },
                        )
                        return 0

                active_runs, kill_handled = process_kill_requests(
                    layout,
                    active_runs,
                    enabled=args.enable_compat_kill_queue,
                )
                if kill_handled:
                    last_control_action = "kill_signal"

                if not stop_when_idle:
                    current_gpu_status = probe_gpu_status(managed_gpu_indices)
                    current_gpu_processes = probe_gpu_processes(managed_gpu_indices, active_runs)
                    current_gpu_availability = summarize_gpu_availability(
                        managed_gpu_indices,
                        active_runs,
                        gpu_status=current_gpu_status,
                        gpu_processes=current_gpu_processes,
                        foreign_gpu_memory_threshold_mb=args.foreign_gpu_memory_threshold_mb,
                    )
                    active_runs = dispatch_launchable_jobs(
                        layout,
                        active_runs,
                        managed_gpu_indices,
                        schedulable_gpu_indices=current_gpu_availability["schedulable_gpu_indices"],
                        supervisor_grace_seconds=args.supervisor_grace_seconds,
                        max_new_jobs=1 if args.once and not launched_any else None,
                    )
                    if active_runs:
                        launched_any = True

                now = time.time()
                if now - last_heartbeat >= args.heartbeat_seconds:
                    gpu_status = probe_gpu_status(managed_gpu_indices)
                    gpu_processes = probe_gpu_processes(managed_gpu_indices, active_runs)
                    gpu_availability = summarize_gpu_availability(
                        managed_gpu_indices,
                        active_runs,
                        gpu_status=gpu_status,
                        gpu_processes=gpu_processes,
                        foreign_gpu_memory_threshold_mb=args.foreign_gpu_memory_threshold_mb,
                    )
                    payload = controller_payload(
                        layout,
                        active_runs,
                        managed_gpu_indices=managed_gpu_indices,
                        gpu_availability=gpu_availability,
                        gpu_status=gpu_status,
                        gpu_processes=gpu_processes,
                        stop_when_idle=stop_when_idle,
                        last_control_action=last_control_action,
                        acquired_at_epoch=acquired_at_epoch,
                    )
                    write_controller_state_snapshots(layout, payload, acquired_at_epoch=acquired_at_epoch)
                    refresh_controller_lease(
                        layout,
                        heartbeat_seconds=args.heartbeat_seconds,
                        acquired_at_epoch=acquired_at_epoch,
                    )
                    append_jsonl(layout.controller_heartbeat, payload)
                    last_heartbeat = now

                if stop_when_idle and not active_runs:
                    gpu_status = probe_gpu_status(managed_gpu_indices)
                    gpu_processes = probe_gpu_processes(managed_gpu_indices, active_runs)
                    gpu_availability = summarize_gpu_availability(
                        managed_gpu_indices,
                        active_runs,
                        gpu_status=gpu_status,
                        gpu_processes=gpu_processes,
                        foreign_gpu_memory_threshold_mb=args.foreign_gpu_memory_threshold_mb,
                    )
                    payload = controller_payload(
                        layout,
                        active_runs,
                        managed_gpu_indices=managed_gpu_indices,
                        gpu_availability=gpu_availability,
                        gpu_status=gpu_status,
                        gpu_processes=gpu_processes,
                        stop_when_idle=stop_when_idle,
                        last_control_action=last_control_action,
                        acquired_at_epoch=acquired_at_epoch,
                    )
                    write_controller_state_snapshots(layout, payload, acquired_at_epoch=acquired_at_epoch)
                    append_controller_event(
                        layout,
                        "controller_stopped",
                        audit=True,
                        message=shutdown_reason or "stop_when_idle",
                        reason=shutdown_reason or "stop_when_idle",
                    )
                    append_jsonl(
                        layout.controller_heartbeat,
                        {
                            **payload,
                            "event": "controller_stopped",
                            "reason": shutdown_reason or "stop_when_idle",
                        },
                    )
                    break

                if args.once and not active_runs and next_visible_json(layout.queue_dir) is None:
                    break

                if args.once and launched_any and not active_runs:
                    break

                exception_backoff_seconds = 1.0
                time.sleep(max(0.2, args.poll_seconds))
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                append_controller_event(
                    layout,
                    "main_loop_exception",
                    audit=True,
                    message=f"{type(exc).__name__}: {exc}",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    backoff_seconds=exception_backoff_seconds,
                )
                time.sleep(exception_backoff_seconds)
                exception_backoff_seconds = min(exception_backoff_seconds * 2.0, 30.0)
        return 0
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        if active_runs:
            active_runs, cancelled_job_ids = cancel_matching_active_runs(
                layout,
                active_runs,
                request_id=f"controller_exit_{int(time.time())}",
                reason=shutdown_reason or "controller_exit_cleanup",
                signal_name="TERM",
                grace_seconds=args.supervisor_grace_seconds,
                job_ids=[],
                control_action="controller_shutdown",
                target_dir=layout.cancelled_dir,
                request={
                    "requested_by": "controller",
                    "requested_from_host": socket.gethostname(),
                    "requested_from_pid": os.getpid(),
                    "request_source": "controller_finalizer",
                },
            )
            if cancelled_job_ids:
                append_controller_event(
                    layout,
                    "controller_exit_cleanup",
                    audit=True,
                    message="cancelled active jobs during controller shutdown",
                    cancelled_job_ids=cancelled_job_ids,
                )
        append_controller_event(
            layout,
            "controller_exiting",
            audit=True,
            message=shutdown_reason or "normal_exit",
            reason=shutdown_reason or "normal_exit",
        )
        release_controller_lease(
            layout,
            heartbeat_seconds=args.heartbeat_seconds,
            acquired_at_epoch=acquired_at_epoch,
        )


if __name__ == "__main__":
    raise SystemExit(main())
