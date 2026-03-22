from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dashboard_server import create_server
from runtime_io import write_json_atomic


def write_fake_runtime(sandbox_root: Path) -> Path:
    runtime_root = sandbox_root / "runtime" / "platform_t-20260322-demo"
    for path in [
        runtime_root / "state",
        runtime_root / "logs" / "jobs",
        runtime_root / "jobs" / "queue",
        runtime_root / "jobs" / "running",
        runtime_root / "jobs" / "done",
        runtime_root / "jobs" / "failed",
        runtime_root / "jobs" / "cancelled",
        runtime_root / "jobs" / "kill",
        runtime_root / "control" / "queue",
        runtime_root / "control" / "done",
        runtime_root / "control" / "failed",
    ]:
        path.mkdir(parents=True, exist_ok=True)

    write_json_atomic(
        sandbox_root / "runtime" / "current_runtime.json",
        {
            "runtime_root": str(runtime_root),
            "updated_at_epoch": time.time(),
        },
    )
    write_json_atomic(
        runtime_root / "state" / "controller_state.json",
        {
            "updated_at_epoch": time.time(),
            "pid": 1234,
            "hostname": "dev-intern-02",
            "runtime_root": str(runtime_root),
            "queue_count": 1,
            "done_count": 0,
            "failed_count": 0,
            "cancelled_count": 0,
            "control_queue_count": 0,
            "managed_gpu_indices": ["0", "1"],
            "active_jobs": [],
            "gpu_status": [
                {"index": 0, "util_pct": 33, "mem_used_mb": 1200, "mem_total_mb": 81920, "temp_c": 40},
                {"index": 1, "util_pct": 0, "mem_used_mb": 2, "mem_total_mb": 81920, "temp_c": 30},
            ],
            "gpu_processes": [],
        },
    )
    write_json_atomic(
        runtime_root / "state" / "controller_lease.json",
        {
            "updated_at_epoch": time.time(),
            "pid": 1234,
            "runtime_root": str(runtime_root),
        },
    )
    (runtime_root / "logs" / "controller_heartbeat.jsonl").write_text(
        json.dumps({"updated_at_epoch": time.time(), "queue_count": 1}) + "\n",
        encoding="utf-8",
    )
    (runtime_root / "logs" / "controller_events.jsonl").write_text(
        json.dumps({"updated_at_epoch": time.time(), "event": "controller_started"}) + "\n",
        encoding="utf-8",
    )
    write_json_atomic(runtime_root / "jobs" / "queue" / "queued.json", {"job_id": "queued-demo"})
    return runtime_root


class DashboardServerTest(unittest.TestCase):
    def start_server(self, sandbox_root: Path, *, allow_write_actions: bool):
        args = SimpleNamespace(
            root=sandbox_root,
            sandbox_root=None,
            host="127.0.0.1",
            port=0,
            allow_write_actions=allow_write_actions,
            default_history_tail=300,
            default_log_tail=400,
        )
        server = create_server(args)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def fetch_json(self, url: str, *, method: str = "GET", payload: dict | None = None) -> dict:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(url, method=method, data=data, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_summary_endpoint_reads_fake_runtime(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="node-controller-dashboard-") as tmpdir:
            sandbox_root = Path(tmpdir)
            runtime_root = write_fake_runtime(sandbox_root)
            server, thread = self.start_server(sandbox_root, allow_write_actions=False)
            try:
                host, port = server.server_address[:2]
                payload = self.fetch_json(f"http://{host}:{port}/api/summary")
                self.assertEqual(Path(payload["runtime_root"]).resolve(), runtime_root.resolve())
                self.assertFalse(payload["allow_write_actions"])
                self.assertEqual(payload["counts"]["queue"], 1)
                self.assertIn("recent_controller_events", payload)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_control_endpoint_can_be_enabled(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="node-controller-dashboard-") as tmpdir:
            sandbox_root = Path(tmpdir)
            runtime_root = write_fake_runtime(sandbox_root)
            server, thread = self.start_server(sandbox_root, allow_write_actions=True)
            try:
                host, port = server.server_address[:2]
                payload = self.fetch_json(
                    f"http://{host}:{port}/api/jobs/demo-job/kill",
                    method="POST",
                    payload={},
                )
                request_path = Path(payload["request_path"])
                self.assertTrue(request_path.exists())
                request = json.loads(request_path.read_text(encoding="utf-8"))
                self.assertEqual(request["action"], "cancel_active_job")
                self.assertEqual(request["job_ids"], ["demo-job"])

                history = self.fetch_json(f"http://{host}:{port}/api/history?tail=10")
                self.assertEqual(Path(history["runtime_root"]).resolve(), runtime_root.resolve())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_write_actions_disabled_returns_403(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="node-controller-dashboard-") as tmpdir:
            sandbox_root = Path(tmpdir)
            write_fake_runtime(sandbox_root)
            server, thread = self.start_server(sandbox_root, allow_write_actions=False)
            try:
                host, port = server.server_address[:2]
                with self.assertRaises(HTTPError) as ctx:
                    self.fetch_json(
                        f"http://{host}:{port}/api/jobs/demo-job/kill",
                        method="POST",
                        payload={},
                    )
                self.assertEqual(ctx.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
