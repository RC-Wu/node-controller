from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR_SCRIPT = REPO_ROOT / "job_supervisor.py"


def load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(os.name != "nt" and hasattr(os, "killpg"), "POSIX process groups required")
class JobSupervisorTest(unittest.TestCase):
    def test_terminate_process_tree_reaps_group_after_leader_exit(self) -> None:
        module = load_module(SUPERVISOR_SCRIPT, "job_supervisor_group_reap_test")
        tmpdir = tempfile.mkdtemp(prefix="node-controller-job-supervisor-")
        child_pid_path = Path(tmpdir) / "ignored_child.pid"
        proc = subprocess.Popen(
            [
                "bash",
                "-lc",
                (
                    "trap 'exit 0' TERM; "
                    f"{shutil.which('python3') or sys.executable} -c "
                    "\"import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)\" "
                    f"& echo $! > {child_pid_path} ; "
                    "wait"
                ),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        child_pid = None
        try:
            deadline = time.time() + 10.0
            while time.time() < deadline:
                if child_pid_path.exists():
                    child_pid = int(child_pid_path.read_text(encoding="utf-8").strip())
                    break
                time.sleep(0.1)
            self.assertIsNotNone(child_pid, "timed out waiting for ignored child pid")
            assert child_pid is not None
            self.assertTrue(module.process_exists(child_pid))

            module.terminate_process_tree(proc, signal_name="TERM", grace_seconds=0.5)

            time.sleep(0.2)
            self.assertFalse(
                module.process_exists(child_pid),
                "child process survived after process-group termination",
            )
        finally:
            if child_pid is not None and module.process_exists(child_pid):
                try:
                    os.kill(child_pid, 9)
                except OSError:
                    pass
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
