#!/usr/bin/env python3
"""
Job supervisor — forked per-job process that manages a single training run.

Responsibilities:
  - Set up environment (CUDA_VISIBLE_DEVICES, etc.)
  - Launch the training command as a subprocess
  - Monitor for timeout
  - Write job log (stdout/stderr)
  - Write result JSON on completion
  - Kill process group on SIGTERM (forwarded from controller)

The controller never touches any of this — it just fork+execs this script
and monitors the supervisor PID. If this process crashes, only this job dies.

Usage (called by controller, not directly):
  python job_supervisor.py --spec /path/to/job_spec.json \
                           --log-dir /path/to/logs/jobs \
                           --result-dir /path/to/done_or_failed \
                           --gpus 0,1,2,3
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Per-job supervisor process.")
    ap.add_argument("--spec", type=Path, required=True, help="Job spec JSON file.")
    ap.add_argument("--log-dir", type=Path, required=True, help="Directory for job log files.")
    ap.add_argument("--result-dir-done", type=Path, required=True, help="Directory for completed jobs.")
    ap.add_argument("--result-dir-failed", type=Path, required=True, help="Directory for failed jobs.")
    ap.add_argument("--gpus", type=str, required=True, help="Comma-separated GPU indices.")
    return ap.parse_args()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


class JobSupervisor:
    def __init__(self, spec: dict[str, Any], gpus: str, log_dir: Path,
                 result_dir_done: Path, result_dir_failed: Path):
        self.spec = spec
        self.job_id = str(spec["job_id"])
        self.gpus = gpus
        self.log_dir = log_dir
        self.result_dir_done = result_dir_done
        self.result_dir_failed = result_dir_failed
        self.proc: subprocess.Popen | None = None
        self.log_handle = None
        self.should_kill = False
        self.started_at = 0.0

        # Forward SIGTERM to child process group
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        signal.signal(signal.SIGINT, self._handle_sigterm)

    def _handle_sigterm(self, signum, frame):
        """Controller sent us SIGTERM — kill our child and exit."""
        self.should_kill = True
        self._kill_child("supervisor_received_signal")

    def _kill_child(self, reason: str) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        pgid = None
        try:
            pgid = os.getpgid(self.proc.pid)
        except (ProcessLookupError, PermissionError):
            pass

        # SIGTERM first
        try:
            if pgid:
                os.killpg(pgid, signal.SIGTERM)
            else:
                self.proc.terminate()
        except (ProcessLookupError, PermissionError):
            pass

        try:
            self.proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass

        # SIGKILL
        try:
            if pgid:
                os.killpg(pgid, signal.SIGKILL)
            else:
                self.proc.kill()
        except (ProcessLookupError, PermissionError):
            pass

        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass

    def run(self) -> int:
        """Launch the job, monitor it, write result. Returns exit code."""
        self.started_at = time.time()
        log_path = self.log_dir / f"{self.job_id}.log"

        # Build environment
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in dict(self.spec.get("env", {})).items()})
        env["CUDA_VISIBLE_DEVICES"] = self.gpus

        # Build command
        command = [str(x) for x in self.spec["command"]]
        orig_command = self.spec["command"]

        # Wrap with stdbuf for line-buffered stdout
        stdbuf_path = shutil.which("stdbuf")
        if stdbuf_path and command:
            command = [stdbuf_path, "-oL", "-eL"] + command

        # Resolve python path
        if orig_command and orig_command[0] in {"python", "python3"}:
            idx = len(command) - len(orig_command)
            resolved = shutil.which(command[idx]) or sys.executable
            command[idx] = resolved

        workdir = Path(str(self.spec.get("workdir", "."))).resolve()
        timeout_seconds = float(self.spec.get("timeout_seconds", 0) or 0)

        # Open log file
        self.log_handle = log_path.open("a", encoding="utf-8")

        # Write supervisor start marker
        self.log_handle.write(
            f"\n{'='*60}\n"
            f"[supervisor] job={self.job_id} gpus={self.gpus} "
            f"start={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"[supervisor] command={command}\n"
            f"[supervisor] workdir={workdir}\n"
            f"{'='*60}\n\n"
        )
        self.log_handle.flush()

        # Launch in its own process group
        try:
            self.proc = subprocess.Popen(
                command,
                cwd=str(workdir),
                env=env,
                stdout=self.log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                preexec_fn=os.setsid,
            )
        except Exception as e:
            self.log_handle.write(f"\n[supervisor] LAUNCH FAILED: {e}\n")
            self.log_handle.flush()
            self._write_result(returncode=-1, status="launch_failed")
            return 1

        # Monitor loop
        while True:
            if self.should_kill:
                break

            # Timeout check
            if timeout_seconds > 0 and (time.time() - self.started_at) > timeout_seconds:
                self.log_handle.write(
                    f"\n[supervisor] TIMEOUT after {timeout_seconds:.0f}s — killing job\n"
                )
                self.log_handle.flush()
                self._kill_child("timeout")
                break

            rc = self.proc.poll()
            if rc is not None:
                break

            time.sleep(1)

        # Wait for process to fully exit
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

        rc = self.proc.poll()
        status = "killed" if self.should_kill else ("done" if rc == 0 else "failed")

        # Write end marker to log
        elapsed = time.time() - self.started_at
        self.log_handle.write(
            f"\n{'='*60}\n"
            f"[supervisor] job={self.job_id} status={status} rc={rc} "
            f"elapsed={elapsed:.1f}s\n"
            f"{'='*60}\n"
        )
        self.log_handle.flush()

        self._write_result(returncode=rc, status=status)

        # Close log
        if self.log_handle and not self.log_handle.closed:
            self.log_handle.close()

        # Return 0 for done, 1 for anything else (controller uses this)
        return 0 if status == "done" else 1

    def _write_result(self, returncode: int | None, status: str) -> None:
        """Write result JSON to done/ or failed/ directory."""
        elapsed = time.time() - self.started_at
        result = {
            "job_id": self.job_id,
            "command": self.spec["command"],
            "gpus": self.gpus,
            "started_at_epoch": self.started_at,
            "finished_at_epoch": time.time(),
            "elapsed_seconds": elapsed,
            "returncode": returncode,
            "status": status,
            "supervisor_pid": os.getpid(),
        }
        target_dir = self.result_dir_done if status == "done" else self.result_dir_failed
        try:
            write_json(target_dir / f"{self.job_id}.result.json", result)
        except OSError as e:
            # Last resort: write to stderr
            print(f"[supervisor] FAILED to write result for {self.job_id}: {e}", file=sys.stderr)


def main() -> int:
    args = parse_args()

    spec = json.loads(args.spec.read_text(encoding="utf-8"))

    supervisor = JobSupervisor(
        spec=spec,
        gpus=args.gpus,
        log_dir=args.log_dir,
        result_dir_done=args.result_dir_done,
        result_dir_failed=args.result_dir_failed,
    )

    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
