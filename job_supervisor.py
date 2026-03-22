#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Per-job supervisor for node-controller.")
    ap.add_argument("--spec", type=Path, required=True, help="Running job spec path.")
    ap.add_argument("--meta", type=Path, required=True, help="Running job meta path.")
    ap.add_argument("--log-path", type=Path, required=True, help="Per-job stdout/stderr log path.")
    ap.add_argument("--event-path", type=Path, required=True, help="Per-job structured event log path.")
    ap.add_argument("--grace-seconds", type=float, default=15.0)
    return ap.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def current_process_group_id() -> int:
    getter = getattr(os, "getpgrp", None)
    if getter is None:
        return os.getpid()
    try:
        return int(getter())
    except Exception:
        return os.getpid()


def terminate_process_tree(proc: subprocess.Popen[str], *, signal_name: str, grace_seconds: float) -> int | None:
    if proc.poll() is not None:
        return proc.poll()
    sig = signal_from_name(signal_name)
    if os.name != "nt" and hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, sig)
        except Exception:
            try:
                proc.send_signal(sig)
            except Exception:
                pass
    else:
        try:
            proc.send_signal(sig)
        except Exception:
            pass
    deadline = time.time() + max(0.0, grace_seconds)
    while time.time() < deadline:
        rc = proc.poll()
        if rc is not None:
            return rc
        time.sleep(0.2)
    if os.name != "nt" and hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        return proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return proc.poll()


def maybe_wrap_stdbuf(command: list[str]) -> tuple[list[str], bool]:
    stdbuf_path = shutil.which("stdbuf")
    if not stdbuf_path or not command:
        return command, False
    return [stdbuf_path, "-oL", "-eL", *command], True


class JobSupervisor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.spec = load_json(args.spec)
        self.meta_path = args.meta
        self.event_path = args.event_path
        self.log_path = args.log_path
        self.job_id = str(self.spec.get("job_id") or args.spec.stem)
        self.child: subprocess.Popen[str] | None = None
        self.child_start_time_ticks: int | None = None
        self.stop_reason = ""
        self.stop_signal = ""
        self.timeout_triggered = False
        self.started_at_epoch = time.time()
        self.last_meta = self._load_meta()

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _load_meta(self) -> dict[str, Any]:
        try:
            payload = load_json(self.meta_path)
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload.setdefault("schema_version", SCHEMA_VERSION)
        payload.setdefault("job_id", self.job_id)
        return payload

    def update_meta(self, **fields: Any) -> dict[str, Any]:
        payload = self._load_meta()
        payload.update(fields)
        write_json_atomic(self.meta_path, payload)
        self.last_meta = payload
        return payload

    def emit(self, event: str, **fields: Any) -> None:
        append_jsonl(
            self.event_path,
            {
                "schema_version": SCHEMA_VERSION,
                "event": event,
                "job_id": self.job_id,
                "updated_at_epoch": time.time(),
                "hostname": socket.gethostname(),
                "supervisor_pid": os.getpid(),
                **fields,
            },
        )

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        self.stop_reason = self.stop_reason or "supervisor_received_signal"
        self.stop_signal = signal.Signals(signum).name
        self.emit("supervisor_signal_received", signal=self.stop_signal, reason=self.stop_reason)
        self.kill_child(signal_name=self.stop_signal, reason=self.stop_reason)

    def kill_child(self, *, signal_name: str, reason: str) -> int | None:
        if self.child is None or self.child.poll() is not None:
            return None if self.child is None else self.child.poll()
        return terminate_process_tree(self.child, signal_name=signal_name, grace_seconds=self.args.grace_seconds)

    def _build_child_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in dict(self.spec.get("env", {})).items()})
        allocated = [str(item) for item in self.last_meta.get("allocated_gpu_indices") or []]
        if allocated:
            env["ALLOCATED_GPU_INDICES"] = ",".join(allocated)
            env["ALLOCATED_GPU_COUNT"] = str(len(allocated))
            if bool(self.last_meta.get("set_cuda_visible_devices")):
                env["CUDA_VISIBLE_DEVICES"] = ",".join(allocated)
        return env

    def _resolve_command(self) -> tuple[list[str], bool]:
        command = [str(part) for part in self.spec.get("command") or []]
        if command and command[0] in {"python", "python3"}:
            command[0] = shutil.which(command[0]) or sys.executable
        return maybe_wrap_stdbuf(command)

    def _final_status(self, returncode: int | None) -> str:
        if self.stop_reason:
            return "cancelled"
        if self.timeout_triggered:
            return "failed"
        return "done" if returncode == 0 else "failed"

    def run(self) -> int:
        original_command = [str(part) for part in self.spec.get("command") or []]
        resolved_command, stdbuf_enabled = self._resolve_command()
        workdir = Path(str(self.spec.get("workdir") or self.args.spec.parent)).resolve()
        timeout_seconds = float(self.spec.get("timeout_seconds") or 0.0)

        self.update_meta(
            supervisor_pid=os.getpid(),
            supervisor_pgid=current_process_group_id(),
            supervisor_start_time_ticks=read_process_start_time_ticks(os.getpid()),
            supervisor_state="starting",
            supervisor_started_at_epoch=self.started_at_epoch,
            event_log_path=str(self.event_path),
            log_path=str(self.log_path),
            original_command=original_command,
            resolved_command=resolved_command,
            stdbuf_enabled=stdbuf_enabled,
        )
        self.emit(
            "supervisor_started",
            workdir=str(workdir),
            timeout_seconds=timeout_seconds,
            allocated_gpu_indices=self.last_meta.get("allocated_gpu_indices") or [],
            stdbuf_enabled=stdbuf_enabled,
        )

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as log_handle:
            log_handle.write(
                f"\n{'=' * 72}\n"
                f"[supervisor] job={self.job_id} start={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"[supervisor] original_command={original_command}\n"
                f"[supervisor] resolved_command={resolved_command}\n"
                f"[supervisor] workdir={workdir}\n"
                f"[supervisor] allocated_gpu_indices={self.last_meta.get('allocated_gpu_indices') or []}\n"
                f"{'=' * 72}\n\n"
            )
            log_handle.flush()

            try:
                self.child = subprocess.Popen(
                    resolved_command,
                    cwd=str(workdir),
                    env=self._build_child_env(),
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    start_new_session=True,
                )
            except Exception as exc:
                self.update_meta(
                    supervisor_state="launch_failed",
                    supervisor_finished_at_epoch=time.time(),
                    final_status="failed",
                    final_returncode=None,
                    stop_reason=f"launch_failed: {type(exc).__name__}: {exc}",
                )
                self.emit("child_launch_failed", reason=f"{type(exc).__name__}: {exc}")
                log_handle.write(f"[supervisor] launch failed: {type(exc).__name__}: {exc}\n")
                log_handle.flush()
                return 1

            self.child_start_time_ticks = read_process_start_time_ticks(self.child.pid)
            self.update_meta(
                supervisor_state="child_running",
                child_pid=self.child.pid,
                child_pgid=self.child.pid,
                child_start_time_ticks=self.child_start_time_ticks,
                child_started_at_epoch=time.time(),
            )
            self.emit(
                "child_started",
                child_pid=self.child.pid,
                child_pgid=self.child.pid,
                child_start_time_ticks=self.child_start_time_ticks,
            )

            while True:
                if self.stop_reason and self.child.poll() is None:
                    self.emit("child_stop_requested", signal=self.stop_signal or "TERM", reason=self.stop_reason)
                    self.kill_child(signal_name=self.stop_signal or "TERM", reason=self.stop_reason)

                if timeout_seconds > 0 and (time.time() - self.started_at_epoch) > timeout_seconds and self.child.poll() is None:
                    self.timeout_triggered = True
                    self.stop_reason = self.stop_reason or "timeout"
                    self.emit("child_timeout", timeout_seconds=timeout_seconds)
                    self.kill_child(signal_name="TERM", reason=self.stop_reason)

                returncode = self.child.poll()
                if returncode is not None:
                    final_status = self._final_status(returncode)
                    finished_at_epoch = time.time()
                    self.update_meta(
                        supervisor_state="finished",
                        supervisor_finished_at_epoch=finished_at_epoch,
                        child_finished_at_epoch=finished_at_epoch,
                        final_status=final_status,
                        final_returncode=returncode,
                        stop_reason=self.stop_reason or None,
                        stopped_by_signal=self.stop_signal or None,
                        timeout_triggered=self.timeout_triggered,
                    )
                    self.emit(
                        "child_exited",
                        returncode=returncode,
                        final_status=final_status,
                        stop_reason=self.stop_reason or None,
                        stopped_by_signal=self.stop_signal or None,
                        timeout_triggered=self.timeout_triggered,
                    )
                    log_handle.write(
                        f"\n{'=' * 72}\n"
                        f"[supervisor] job={self.job_id} final_status={final_status} returncode={returncode}\n"
                        f"[supervisor] stop_reason={self.stop_reason or '-'} signal={self.stop_signal or '-'} timeout={self.timeout_triggered}\n"
                        f"{'=' * 72}\n"
                    )
                    log_handle.flush()
                    return 0 if final_status == "done" else 1
                time.sleep(0.5)


def main() -> int:
    args = parse_args()
    return JobSupervisor(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
