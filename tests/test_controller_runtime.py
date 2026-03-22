from __future__ import annotations

import json
import subprocess
import sys
import shutil
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = REPO_ROOT / "controller.py"
SUBMIT_JOB = REPO_ROOT / "submit_job.py"


def wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {path}")


def wait_until_file_released(path: Path, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not path.exists():
            return
        try:
            with path.open("a", encoding="utf-8"):
                return
        except PermissionError:
            time.sleep(0.1)
    raise AssertionError(f"timed out waiting for file release: {path}")


class ControllerRuntimeTest(unittest.TestCase):
    def start_controller(self, root: Path, *, capture: bool = False) -> subprocess.Popen[str]:
        kwargs = {
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "cwd": str(REPO_ROOT),
        }
        if capture:
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.STDOUT
        return subprocess.Popen(
            [
                sys.executable,
                str(CONTROLLER),
                "--root",
                str(root),
                "--poll-seconds",
                "0.1",
                "--heartbeat-seconds",
                "0.1",
            ],
            **kwargs,
        )

    def submit_sleep_job(self, root: Path, *, job_id: str, seconds: float) -> None:
        subprocess.run(
            [
                sys.executable,
                str(SUBMIT_JOB),
                "--root",
                str(root),
                "--job-id",
                job_id,
                "--workdir",
                str(root),
                "--timeout-seconds",
                "30",
                "--",
                sys.executable,
                "-c",
                (
                    "import pathlib,time; "
                    f"pathlib.Path({str(root / 'job_started.txt')!r}).write_text('started', encoding='utf-8'); "
                    f"time.sleep({seconds}); "
                    f"pathlib.Path({str(root / 'job_finished.txt')!r}).write_text('done', encoding='utf-8')"
                ),
            ],
            check=True,
            cwd=str(REPO_ROOT),
            text=True,
        )

    def test_restarts_adopt_running_jobs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="node-controller-restart-") as tmpdir:
            root = Path(tmpdir) / "runtime"
            first = self.start_controller(root)
            try:
                self.submit_sleep_job(root, job_id="sleep_job", seconds=1.5)
                wait_for(root / "jobs" / "running" / "sleep_job.meta.json")
                wait_for(root / "job_started.txt")
                first.kill()
                first.wait(timeout=5)

                second = self.start_controller(root)
                try:
                    result_path = root / "jobs" / "done" / "sleep_job.result.json"
                    wait_for(result_path, timeout=10.0)
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    self.assertEqual(result["status"], "done")
                    self.assertEqual(result.get("returncode"), 0)
                    self.assertEqual(result.get("supervisor_state"), "finished")
                    self.assertTrue((root / "job_finished.txt").exists())
                finally:
                    second.terminate()
                    second.wait(timeout=5)
            finally:
                if first.poll() is None:
                    first.terminate()
                    first.wait(timeout=5)

    def test_lease_blocks_second_controller(self) -> None:
        with tempfile.TemporaryDirectory(prefix="node-controller-lease-") as tmpdir:
            root = Path(tmpdir) / "runtime"
            first = self.start_controller(root)
            try:
                wait_for(root / "state" / "controller_lease.json")
                wait_for(root / "state" / "controller_state.json")
                state = json.loads((root / "state" / "controller_state.json").read_text(encoding="utf-8"))
                self.assertIn("gpu_status", state)
                self.assertIsInstance(state["gpu_status"], list)
                second = self.start_controller(root, capture=True)
                output, _ = second.communicate(timeout=5)
                self.assertNotEqual(second.returncode, 0)
                self.assertIn("controller lease already held", output)
            finally:
                if first.poll() is None:
                    first.terminate()
                    first.wait(timeout=5)

    def test_kill_signal_cancels_running_job(self) -> None:
        tmpdir = tempfile.mkdtemp(prefix="node-controller-kill-")
        root = Path(tmpdir) / "runtime"
        controller = self.start_controller(root)
        try:
            self.submit_sleep_job(root, job_id="kill_me", seconds=30.0)
            wait_for(root / "jobs" / "running" / "kill_me.meta.json")
            subprocess.run(
                [
                    sys.executable,
                    str(SUBMIT_JOB),
                    "--root",
                    str(root),
                    "--kill",
                    "kill_me",
                ],
                check=True,
                cwd=str(REPO_ROOT),
                text=True,
            )
            result_path = root / "jobs" / "cancelled" / "kill_me.result.json"
            wait_for(result_path, timeout=20.0)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(result["control_action"], "cancel_active_job")
        finally:
            if controller.poll() is None:
                controller.terminate()
                controller.wait(timeout=5)
            wait_until_file_released(root / "logs" / "jobs" / "kill_me.log", timeout=10.0)
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_shared_runtime_pointer_and_status_summary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="node-controller-pointer-") as tmpdir:
            root = Path(tmpdir) / "runtime"
            shared_root = root.parent
            controller = self.start_controller(root)
            try:
                wait_for(shared_root / "current_runtime.json")
                wait_for(shared_root / "state" / "controller_state.json")
                pointer = json.loads((shared_root / "current_runtime.json").read_text(encoding="utf-8"))
                self.assertEqual(Path(pointer["runtime_root"]).resolve(), root.resolve())
                self.assertEqual(pointer["runtime_name"], root.name)

                summary = subprocess.run(
                    [
                        sys.executable,
                        str(SUBMIT_JOB),
                        "--root",
                        str(shared_root),
                        "--status-summary",
                    ],
                    check=True,
                    cwd=str(REPO_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                self.assertIn(f"runtime_root: {root.resolve()}", summary.stdout)
                self.assertIn("gpu_status:", summary.stdout)
            finally:
                if controller.poll() is None:
                    controller.terminate()
                    controller.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
