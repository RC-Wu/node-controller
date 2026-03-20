#!/usr/bin/env python3
"""
Node controller v3 — slurm-style fork-based job scheduler.

Architecture (inspired by slurmd/slurmstepd):
  controller.py (immortal daemon — never touches job code)
    └── fork → job_supervisor.py (per-job, isolated process)
                  └── exec → training command

The controller ONLY does:
  - Poll the queue directory for job specs
  - Manage GPU pool allocation
  - Fork supervisor processes (never runs job code itself)
  - Monitor supervisor PIDs via os.waitpid(WNOHANG)
  - Write heartbeat state (with GPU status from nvidia-smi)
  - Handle kill signals by sending SIGTERM to supervisors
  - Graceful shutdown on SIGTERM (kills all supervisors)

If a training job crashes → supervisor catches it, writes result, exits.
If a supervisor crashes → controller sees dead PID, releases GPUs.
The controller itself has no exposure to job-related failures.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ─── Logging ─────────────────────────────────────────────────────────────────

def setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("controller")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(str(log_path), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


log = logging.getLogger("controller")

SUPERVISOR_SCRIPT = Path(__file__).parent / "job_supervisor.py"


# ─── CLI ─────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Fork-based GPU job scheduler (slurm-style).")
    ap.add_argument("--root", type=Path, required=True, help="Runtime root on vePFS.")
    ap.add_argument("--total-gpus", type=int, default=8)
    ap.add_argument("--poll-seconds", type=float, default=2.0)
    ap.add_argument("--heartbeat-seconds", type=float, default=10.0)
    ap.add_argument("--once", action="store_true")
    return ap.parse_args()


# ─── JSON helpers ────────────────────────────────────────────────────────────

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


# ─── VEPFS health ────────────────────────────────────────────────────────────

def check_vepfs_writable(test_dir: Path) -> bool:
    probe = test_dir / ".controller_probe"
    try:
        probe.write_text(f"{time.time()}\n")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def wait_for_vepfs(test_dir: Path, check_interval: float = 30.0) -> None:
    if check_vepfs_writable(test_dir):
        return
    log.warning("VEPFS not writable — pausing until resolved...")
    while True:
        time.sleep(check_interval)
        if check_vepfs_writable(test_dir):
            log.info("VEPFS writable again. Resuming.")
            return
        log.warning("VEPFS still not writable. Retrying in %.0fs...", check_interval)


def safe_write_json(path: Path, payload: dict[str, Any], vepfs_root: Path) -> bool:
    wait_for_vepfs(vepfs_root)
    try:
        write_json(path, payload)
        return True
    except OSError as e:
        log.error("Failed to write %s: %s", path, e)
        return False


def safe_append_jsonl(path: Path, payload: dict[str, Any], vepfs_root: Path) -> bool:
    wait_for_vepfs(vepfs_root)
    try:
        append_jsonl(path, payload)
        return True
    except OSError as e:
        log.error("Failed to append %s: %s", path, e)
        return False


# ─── Layout ──────────────────────────────────────────────────────────────────

@dataclass
class RuntimeLayout:
    root: Path
    queue_dir: Path
    running_dir: Path
    done_dir: Path
    failed_dir: Path
    kill_dir: Path
    logs_dir: Path
    jobs_log_dir: Path
    state_dir: Path
    controller_state: Path
    controller_heartbeat: Path
    controller_log: Path


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
        kill_dir=jobs_root / "kill",
        logs_dir=logs_root,
        jobs_log_dir=logs_root / "jobs",
        state_dir=state_root,
        controller_state=state_root / "controller_state.json",
        controller_heartbeat=logs_root / "controller_heartbeat.jsonl",
        controller_log=logs_root / "controller.log",
    )
    for directory in [
        layout.queue_dir, layout.running_dir, layout.done_dir,
        layout.failed_dir, layout.kill_dir, layout.jobs_log_dir,
        layout.state_dir,
    ]:
        directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(layout.root, 0o755)
        os.chmod(jobs_root, 0o755)
        os.chmod(logs_root, 0o755)
        os.chmod(state_root, 0o755)
        os.chmod(layout.queue_dir, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO | stat.S_ISVTX)
        os.chmod(layout.kill_dir, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO | stat.S_ISVTX)
        for d in [layout.running_dir, layout.done_dir, layout.failed_dir, layout.jobs_log_dir]:
            os.chmod(d, 0o755)
    except PermissionError:
        pass
    return layout


# ─── GPU Pool ────────────────────────────────────────────────────────────────

@dataclass
class GPUPool:
    total: int
    allocated: dict[str, list[int]] = field(default_factory=dict)

    @property
    def free(self) -> list[int]:
        used = set()
        for gpus in self.allocated.values():
            used.update(gpus)
        return sorted(set(range(self.total)) - used)

    @property
    def num_free(self) -> int:
        return len(self.free)

    def allocate(self, job_id: str, num_gpus: int) -> list[int] | None:
        free = self.free
        if len(free) < num_gpus:
            return None
        for start in range(len(free) - num_gpus + 1):
            candidate = free[start:start + num_gpus]
            if candidate[-1] - candidate[0] == num_gpus - 1:
                self.allocated[job_id] = candidate
                return candidate
        selected = free[:num_gpus]
        self.allocated[job_id] = selected
        return selected

    def release(self, job_id: str) -> list[int]:
        return self.allocated.pop(job_id, [])


# ─── GPU status probe ────────────────────────────────────────────────────────

def probe_gpu_status() -> list[dict[str, Any]]:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        gpus = []
        for line in result.stdout.strip().split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                gpus.append({
                    "index": int(parts[0]),
                    "util_pct": int(parts[1]),
                    "mem_used_mb": int(parts[2]),
                    "mem_total_mb": int(parts[3]),
                    "temp_c": int(parts[4]),
                })
        return gpus
    except Exception:
        return []


# ─── Tracked Supervisor ──────────────────────────────────────────────────────

@dataclass
class TrackedSupervisor:
    """Controller's view of a running supervisor — just a PID and metadata."""
    job_id: str
    supervisor_pid: int
    gpus: list[int]
    started_at: float
    spec_path: Path  # in running/


# ─── Job operations (controller side — minimal, no job code) ─────────────────

def scan_queue(layout: RuntimeLayout) -> list[Path]:
    return sorted(layout.queue_dir.glob("*.json"))


def validate_spec(spec_path: Path) -> dict[str, Any]:
    spec = read_json(spec_path)
    if "job_id" not in spec:
        raise ValueError(f"missing job_id")
    if "command" not in spec:
        raise ValueError(f"missing command")
    if not isinstance(spec["command"], list) or not spec["command"]:
        raise ValueError(f"command must be a non-empty list")
    return spec


def fork_supervisor(
    layout: RuntimeLayout,
    spec_path: Path,
    gpus: list[int],
    python_bin: str,
) -> TrackedSupervisor:
    """Move spec to running/, fork a supervisor process, return tracker."""
    spec = validate_spec(spec_path)
    job_id = str(spec["job_id"])
    running_path = layout.running_dir / spec_path.name
    shutil.move(str(spec_path), str(running_path))

    gpu_str = ",".join(str(g) for g in gpus)

    # Fork the supervisor as a completely separate process
    supervisor_cmd = [
        python_bin, "-u", str(SUPERVISOR_SCRIPT),
        "--spec", str(running_path),
        "--log-dir", str(layout.jobs_log_dir),
        "--result-dir-done", str(layout.done_dir),
        "--result-dir-failed", str(layout.failed_dir),
        "--gpus", gpu_str,
    ]

    # Supervisor gets its own process group (so controller SIGTERM doesn't auto-propagate)
    proc = subprocess.Popen(
        supervisor_cmd,
        start_new_session=True,  # new session = new process group
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    now = time.time()
    log.info("Forked supervisor for job %s: pid=%d, gpus=%s", job_id, proc.pid, gpus)

    # Write meta for observability
    meta = {
        "job_id": job_id,
        "supervisor_pid": proc.pid,
        "gpus": gpus,
        "started_at_epoch": now,
        "spec_path": str(running_path),
    }
    try:
        write_json(layout.running_dir / f"{job_id}.meta.json", meta)
    except OSError as e:
        log.warning("Failed to write meta for %s: %s", job_id, e)

    return TrackedSupervisor(
        job_id=job_id,
        supervisor_pid=proc.pid,
        gpus=gpus,
        started_at=now,
        spec_path=running_path,
    )


def reap_supervisor(sv: TrackedSupervisor) -> int | None:
    """Non-blocking check if supervisor exited. Returns exit code or None."""
    try:
        pid, status = os.waitpid(sv.supervisor_pid, os.WNOHANG)
        if pid == 0:
            return None  # still running
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return -os.WTERMSIG(status)
        return -1
    except ChildProcessError:
        # PID already reaped or not our child
        return -1


def kill_supervisor(sv: TrackedSupervisor, reason: str = "killed") -> None:
    """Send SIGTERM to supervisor (which forwards to its child)."""
    log.info("Sending SIGTERM to supervisor %s (pid=%d, reason=%s)", sv.job_id, sv.supervisor_pid, reason)
    try:
        os.kill(sv.supervisor_pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError) as e:
        log.debug("SIGTERM to supervisor %s: %s", sv.job_id, e)

    # Wait briefly for graceful exit
    deadline = time.time() + 8
    while time.time() < deadline:
        rc = reap_supervisor(sv)
        if rc is not None:
            return
        time.sleep(0.5)

    # Force kill
    log.warning("Supervisor %s didn't exit after SIGTERM, sending SIGKILL", sv.job_id)
    try:
        os.kill(sv.supervisor_pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    # Final reap
    try:
        os.waitpid(sv.supervisor_pid, 0)
    except ChildProcessError:
        pass


# ─── Kill signal check ──────────────────────────────────────────────────────

def check_kill_signals(layout: RuntimeLayout) -> set[str]:
    kill_ids = set()
    try:
        for f in layout.kill_dir.glob("*"):
            kill_ids.add(f.stem)
    except OSError:
        pass
    return kill_ids


def clear_kill_signal(layout: RuntimeLayout, job_id: str) -> None:
    try:
        for f in layout.kill_dir.glob(f"{job_id}*"):
            f.unlink(missing_ok=True)
    except OSError:
        pass


# ─── Cleanup after supervisor exit ──────────────────────────────────────────

def cleanup_after_supervisor(layout: RuntimeLayout, sv: TrackedSupervisor, rc: int) -> None:
    """Clean up running/ dir entries after supervisor exits.
    The supervisor itself writes the result JSON to done/ or failed/.
    We just need to move the spec and meta from running/."""
    status = "done" if rc == 0 else "failed"
    target_dir = layout.done_dir if rc == 0 else layout.failed_dir

    try:
        if sv.spec_path.exists():
            shutil.move(str(sv.spec_path), str(target_dir / sv.spec_path.name))
    except OSError as e:
        log.warning("Failed to move spec for %s: %s", sv.job_id, e)

    meta_path = layout.running_dir / f"{sv.job_id}.meta.json"
    try:
        if meta_path.exists():
            shutil.move(str(meta_path), str(target_dir / meta_path.name))
    except OSError as e:
        log.warning("Failed to move meta for %s: %s", sv.job_id, e)

    elapsed = time.time() - sv.started_at
    log.info("Supervisor for %s exited: rc=%d status=%s elapsed=%.1fs", sv.job_id, rc, status, elapsed)


# ─── State ───────────────────────────────────────────────────────────────────

def controller_state_payload(
    active: dict[str, TrackedSupervisor],
    gpu_pool: GPUPool,
    gpu_status: list[dict[str, Any]],
    controller_status: str = "running",
) -> dict[str, Any]:
    return {
        "updated_at_epoch": time.time(),
        "pid": os.getpid(),
        "status": controller_status,
        "total_gpus": gpu_pool.total,
        "free_gpus": gpu_pool.free,
        "active_jobs": [
            {
                "job_id": sv.job_id,
                "gpus": sv.gpus,
                "supervisor_pid": sv.supervisor_pid,
                "started_at_epoch": sv.started_at,
                "elapsed_seconds": time.time() - sv.started_at,
            }
            for sv in active.values()
        ],
        "gpu_status": gpu_status,
    }


# ─── Graceful shutdown ──────────────────────────────────────────────────────

class GracefulShutdown:
    def __init__(self):
        self.should_exit = False
        signal.signal(signal.SIGTERM, self._handle)
        signal.signal(signal.SIGINT, self._handle)

    def _handle(self, signum, frame):
        sig_name = signal.Signals(signum).name
        log.warning("Received %s — initiating graceful shutdown", sig_name)
        self.should_exit = True


# ─── Main loop ───────────────────────────────────────────────────────────────

def main() -> int:
    args = parse_args()
    layout = build_layout(args.root.resolve())

    global log
    log = setup_logging(layout.controller_log)

    # Determine python binary for supervisor
    python_bin = sys.executable

    gpu_pool = GPUPool(total=args.total_gpus)
    active: dict[str, TrackedSupervisor] = {}
    last_heartbeat = 0.0
    shutdown = GracefulShutdown()

    log.info("Controller v3 started: pid=%d, gpus=%d, root=%s, supervisor=%s",
             os.getpid(), args.total_gpus, args.root, SUPERVISOR_SCRIPT)

    try:
        while not shutdown.should_exit:
            try:
                now = time.time()

                # ── Heartbeat ──
                if now - last_heartbeat >= args.heartbeat_seconds:
                    gpu_status = probe_gpu_status()
                    payload = controller_state_payload(active, gpu_pool, gpu_status)
                    safe_write_json(layout.controller_state, payload, layout.root)
                    safe_append_jsonl(layout.controller_heartbeat, payload, layout.root)
                    last_heartbeat = now

                # ── Kill signals ──
                kill_ids = check_kill_signals(layout)
                for job_id in kill_ids:
                    if job_id in active:
                        sv = active[job_id]
                        log.info("Kill signal for job %s", job_id)
                        kill_supervisor(sv, reason="killed_by_signal")
                        cleanup_after_supervisor(layout, sv, rc=1)
                        gpu_pool.release(job_id)
                        del active[job_id]
                        safe_append_jsonl(layout.controller_heartbeat, {
                            "event": "job_killed",
                            "job_id": job_id,
                            "updated_at_epoch": time.time(),
                        }, layout.root)
                    clear_kill_signal(layout, job_id)

                # ── Reap finished supervisors ──
                finished = []
                for job_id, sv in active.items():
                    rc = reap_supervisor(sv)
                    if rc is not None:
                        finished.append((job_id, rc))

                for job_id, rc in finished:
                    sv = active.pop(job_id)
                    cleanup_after_supervisor(layout, sv, rc)
                    gpu_pool.release(job_id)
                    safe_append_jsonl(layout.controller_heartbeat, {
                        "event": "job_finished",
                        "job_id": job_id,
                        "supervisor_rc": rc,
                        "updated_at_epoch": time.time(),
                    }, layout.root)

                # ── Launch queued jobs ──
                if not shutdown.should_exit:
                    for spec_path in scan_queue(layout):
                        try:
                            spec = validate_spec(spec_path)
                        except (ValueError, json.JSONDecodeError) as e:
                            log.error("Bad spec %s: %s", spec_path, e)
                            try:
                                shutil.move(str(spec_path), str(layout.failed_dir / spec_path.name))
                            except OSError:
                                pass
                            continue

                        job_id = str(spec["job_id"])
                        num_gpus = int(spec.get("gpus", args.total_gpus))

                        if job_id in active:
                            log.warning("Skip %s: already running", job_id)
                            try:
                                spec_path.unlink(missing_ok=True)
                            except OSError:
                                pass
                            continue

                        allocated = gpu_pool.allocate(job_id, num_gpus)
                        if allocated is None:
                            continue

                        try:
                            sv = fork_supervisor(layout, spec_path, allocated, python_bin)
                            active[job_id] = sv
                            safe_append_jsonl(layout.controller_heartbeat, {
                                "event": "job_started",
                                "job_id": job_id,
                                "gpus": allocated,
                                "supervisor_pid": sv.supervisor_pid,
                                "updated_at_epoch": time.time(),
                            }, layout.root)
                        except Exception:
                            log.exception("Failed to fork supervisor for %s", job_id)
                            gpu_pool.release(job_id)
                            running_path = layout.running_dir / spec_path.name
                            target = running_path if running_path.exists() else spec_path
                            try:
                                if target.exists():
                                    shutil.move(str(target), str(layout.failed_dir / spec_path.name))
                            except OSError:
                                pass

                # ── Once mode ──
                if args.once and not active and not list(layout.queue_dir.glob("*.json")):
                    break

            except Exception:
                log.exception("Unhandled exception in main loop — recovering")
                time.sleep(5)

            time.sleep(max(0.2, args.poll_seconds))

    finally:
        # Graceful shutdown: kill all supervisors
        if active:
            log.warning("Shutting down — killing %d supervisor(s)", len(active))
            for job_id, sv in list(active.items()):
                kill_supervisor(sv, reason="controller_shutdown")
                cleanup_after_supervisor(layout, sv, rc=1)
                gpu_pool.release(job_id)
            active.clear()
            log.info("All supervisors killed.")

        # Final state
        gpu_status = probe_gpu_status()
        payload = controller_state_payload(active, gpu_pool, gpu_status, controller_status="shutdown")
        safe_write_json(layout.controller_state, payload, layout.root)
        log.info("Controller exiting.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
