#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = REPO_ROOT / "controller.py"
SUBMIT_JOB = REPO_ROOT / "submit_job.py"


def run(*args: str, timeout: float = 30.0, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=check,
    )


def wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for {path}")


def wait_for_text(path: Path, needle: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if needle in text:
                return
        time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for {needle!r} in {path}")


def test_queue_to_done(tmp_root: Path) -> None:
    runtime = tmp_root / "queue_to_done"
    run(
        sys.executable,
        str(SUBMIT_JOB),
        "--root",
        str(runtime),
        "--job-id",
        "smoke_ok",
        "--",
        sys.executable,
        "-c",
        "print('smoke_ok')",
    )
    run(sys.executable, str(CONTROLLER), "--root", str(runtime), "--once", "--poll-seconds", "0.1", "--heartbeat-seconds", "0.1")
    result_path = runtime / "jobs" / "done" / "smoke_ok.result.json"
    if not result_path.exists():
        raise AssertionError("queue_to_done: result missing")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("status") != "done":
        raise AssertionError(f"queue_to_done: unexpected status {result.get('status')!r}")


def test_invalid_queue_json(tmp_root: Path) -> None:
    runtime = tmp_root / "invalid_queue"
    queue_dir = runtime / "jobs" / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    (queue_dir / "bad.json").write_text("{not valid json\n", encoding="utf-8")
    run(sys.executable, str(CONTROLLER), "--root", str(runtime), "--once", "--poll-seconds", "0.1", "--heartbeat-seconds", "0.1")
    result_path = runtime / "jobs" / "failed" / "bad.result.json"
    if not result_path.exists():
        raise AssertionError("invalid_queue: failed result missing")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("status") != "failed":
        raise AssertionError(f"invalid_queue: unexpected status {result.get('status')!r}")


def test_restart_and_reattach(tmp_root: Path) -> None:
    runtime = tmp_root / "reattach"
    run(
        sys.executable,
        str(SUBMIT_JOB),
        "--root",
        str(runtime),
        "--job-id",
        "long_job",
        "--",
        sys.executable,
        "-c",
        "import time; print('long_job_start', flush=True); time.sleep(3); print('long_job_done', flush=True)",
    )
    controller1 = subprocess.Popen(
        [sys.executable, str(CONTROLLER), "--root", str(runtime), "--poll-seconds", "0.1", "--heartbeat-seconds", "0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(runtime / "jobs" / "running" / "long_job.meta.json", timeout=10.0)
        controller1.kill()
        controller1.wait(timeout=10.0)
        shutil.rmtree(runtime / "state" / "controller.lock", ignore_errors=True)
        controller2 = subprocess.Popen(
            [
                sys.executable,
                str(CONTROLLER),
                "--root",
                str(runtime),
                "--once",
                "--poll-seconds",
                "0.1",
                "--heartbeat-seconds",
                "0.1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        controller2.wait(timeout=15.0)
        if controller2.returncode != 0:
            raise AssertionError(f"reattach: controller2 exited with {controller2.returncode}")
        wait_for_text(runtime / "logs" / "controller_heartbeat.jsonl", "job_reattached", timeout=5.0)
        result_path = runtime / "jobs" / "done" / "long_job.result.json"
        if not result_path.exists():
            raise AssertionError("reattach: final result missing")
    finally:
        if controller1.poll() is None:
            controller1.kill()


def test_kill_signal(tmp_root: Path) -> None:
    runtime = tmp_root / "kill_signal"
    run(
        sys.executable,
        str(SUBMIT_JOB),
        "--root",
        str(runtime),
        "--job-id",
        "kill_me",
        "--",
        sys.executable,
        "-c",
        "import time; print('kill_me_start', flush=True); time.sleep(30)",
    )
    controller = subprocess.Popen(
        [sys.executable, str(CONTROLLER), "--root", str(runtime), "--poll-seconds", "0.1", "--heartbeat-seconds", "0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(runtime / "jobs" / "running" / "kill_me.meta.json", timeout=10.0)
        run(
            sys.executable,
            str(SUBMIT_JOB),
            "--root",
            str(runtime),
            "--kill",
            "kill_me",
        )
        result_path = runtime / "jobs" / "cancelled" / "kill_me.result.json"
        wait_for(result_path, timeout=10.0)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result.get("status") != "cancelled":
            raise AssertionError(f"kill_signal: unexpected status {result.get('status')!r}")
        if result.get("control_action") != "cancel_active_job":
            raise AssertionError(f"kill_signal: unexpected control_action {result.get('control_action')!r}")
    finally:
        if controller.poll() is None:
            controller.terminate()
            controller.wait(timeout=10.0)


def test_single_instance_lock(tmp_root: Path) -> None:
    runtime = tmp_root / "single_instance"
    controller1 = subprocess.Popen(
        [sys.executable, str(CONTROLLER), "--root", str(runtime), "--poll-seconds", "0.1", "--heartbeat-seconds", "0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(runtime / "state" / "controller_lease.json", timeout=10.0)
        second = run(
            sys.executable,
            str(CONTROLLER),
            "--root",
            str(runtime),
            "--once",
            "--poll-seconds",
            "0.1",
            "--heartbeat-seconds",
            "0.1",
            timeout=10.0,
            check=False,
        )
        if second.returncode == 0:
            raise AssertionError("single_instance: second controller unexpectedly succeeded")
    finally:
        if controller1.poll() is None:
            controller1.terminate()
            controller1.wait(timeout=10.0)


def main() -> int:
    tmp_root = Path(tempfile.mkdtemp(prefix="node_controller_smoke_"))
    try:
        test_queue_to_done(tmp_root)
        test_invalid_queue_json(tmp_root)
        test_restart_and_reattach(tmp_root)
        test_kill_signal(tmp_root)
        test_single_instance_lock(tmp_root)
        print(f"PASS tmp_root={tmp_root}")
        return 0
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
