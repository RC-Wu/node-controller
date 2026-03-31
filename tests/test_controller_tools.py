from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
STATUS_SCRIPT = REPO_ROOT / "scripts" / "controller_status.py"
SUPERVISOR_SCRIPT = REPO_ROOT / "scripts" / "controller_supervisor.py"
CONTROLLER_SCRIPT = REPO_ROOT / "controller.py"


def load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class ControllerToolingTest(unittest.TestCase):
    def test_adopt_running_jobs_waits_for_bootstrap_meta_settle(self) -> None:
        module = load_module(CONTROLLER_SCRIPT, "controller_bootstrap_settle_test")
        with tempfile.TemporaryDirectory(prefix="node-controller-adopt-") as tmpdir:
            root = Path(tmpdir)
            layout = module.build_layout(root)
            running_spec = layout.running_dir / "bootstrap_job.json"
            running_meta = layout.running_dir / "bootstrap_job.meta.json"
            command = [sys.executable, "-c", "import time; time.sleep(30)"]
            module.write_json(
                running_spec,
                {
                    "schema_version": 2,
                    "job_id": "bootstrap_job",
                    "command": command,
                },
            )
            module.write_json(
                running_meta,
                {
                    "schema_version": 2,
                    "job_id": "bootstrap_job",
                    "command": command,
                    "workdir": str(root),
                    "running_spec_path": str(running_spec),
                    "allocated_gpu_indices": [],
                    "exclusive": False,
                    "supervisor_state": "launching",
                    "controller_launch_pid": os.getpid(),
                    "started_at_epoch": time.time(),
                },
            )
            proc = subprocess.Popen(command, cwd=str(root), start_new_session=True)

            def complete_meta() -> None:
                time.sleep(0.2)
                payload = module.read_json(running_meta)
                payload.update(
                    {
                        "supervisor_pid": proc.pid,
                        "supervisor_pgid": proc.pid,
                        "supervisor_start_time_ticks": module.read_process_start_time_ticks(proc.pid),
                        "supervisor_state": "spawned",
                        "resolved_command": command,
                    }
                )
                module.write_json(running_meta, payload)

            updater = threading.Thread(target=complete_meta, daemon=True)
            updater.start()
            try:
                adopted = module.adopt_running_jobs(layout)
                self.assertEqual(len(adopted), 1)
                self.assertEqual(adopted[0].meta["job_id"], "bootstrap_job")
                self.assertFalse((layout.failed_dir / "bootstrap_job.result.json").exists())
            finally:
                updater.join(timeout=2.0)
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)

    def test_controller_gpu_availability_quarantines_foreign_usage(self) -> None:
        module = load_module(CONTROLLER_SCRIPT, "controller_gpu_availability_test")
        availability = module.summarize_gpu_availability(
            ["0", "1", "2"],
            [],
            gpu_status=[
                {"index": 0, "util_pct": 100, "mem_used_mb": 32000, "mem_total_mb": 81920, "temp_c": 40},
                {"index": 1, "util_pct": 0, "mem_used_mb": 0, "mem_total_mb": 81920, "temp_c": 31},
                {"index": 2, "util_pct": 0, "mem_used_mb": 0, "mem_total_mb": 81920, "temp_c": 30},
            ],
            gpu_processes=[
                {"gpu_index": 0, "pid": 2872525, "process_name": "[Not Found]", "used_memory_mb": 31990, "controller_job_id": None},
            ],
            foreign_gpu_memory_threshold_mb=2048,
        )
        self.assertEqual(availability["externally_blocked_gpu_indices"], ["0"])
        self.assertEqual(availability["schedulable_gpu_indices"], ["1", "2"])
        self.assertEqual(availability["free_gpu_indices"], ["1", "2"])
        self.assertIn("foreign_proc", availability["gpu_external_block_reasons"]["0"][0])

    def test_controller_gpu_availability_keeps_active_gpu_separate_from_foreign_block(self) -> None:
        module = load_module(CONTROLLER_SCRIPT, "controller_gpu_active_untracked_test")
        active_run = SimpleNamespace(allocated_gpu_indices=["2", "3"])
        availability = module.summarize_gpu_availability(
            ["0", "1", "2", "3"],
            [active_run],
            gpu_status=[
                {"index": 0, "util_pct": 95, "mem_used_mb": 48000, "mem_total_mb": 81920, "temp_c": 45},
                {"index": 2, "util_pct": 90, "mem_used_mb": 27000, "mem_total_mb": 81920, "temp_c": 42},
                {"index": 3, "util_pct": 90, "mem_used_mb": 27000, "mem_total_mb": 81920, "temp_c": 43},
            ],
            gpu_processes=[
                {"gpu_index": 0, "pid": 4001, "process_name": "[Not Found]", "used_memory_mb": 47990, "controller_job_id": None},
                {"gpu_index": 2, "pid": 5001, "process_name": "[Not Found]", "used_memory_mb": 26990, "controller_job_id": None},
                {"gpu_index": 3, "pid": 5002, "process_name": "[Not Found]", "used_memory_mb": 26990, "controller_job_id": None},
            ],
            foreign_gpu_memory_threshold_mb=2048,
        )
        self.assertEqual(availability["active_gpu_indices"], ["2", "3"])
        self.assertEqual(availability["externally_blocked_gpu_indices"], ["0"])
        self.assertEqual(availability["active_untracked_gpu_indices"], ["2", "3"])
        self.assertEqual(availability["schedulable_gpu_indices"], ["1", "2", "3"])

    def test_reserve_job_respects_schedulable_gpu_indices(self) -> None:
        module = load_module(CONTROLLER_SCRIPT, "controller_reserve_schedulable_test")
        request = SimpleNamespace(
            exclusive=False,
            requested_gpu_indices=["0", "1"],
            gpu_count=0,
            allowed_gpu_indices=[],
        )
        allocation = module.reserve_job(
            request,
            [],
            ["0", "1", "2", "3"],
            schedulable_gpu_indices=["2", "3"],
        )
        self.assertIsNone(allocation)

    def test_startup_gpu_shortage_wait_mode_stays_alive(self) -> None:
        module = load_module(CONTROLLER_SCRIPT, "controller_startup_wait_mode_test")
        with tempfile.TemporaryDirectory(prefix="node-controller-startup-wait-") as tmpdir:
            root = Path(tmpdir) / "runtime"
            root.mkdir(parents=True, exist_ok=True)
            layout = module.build_layout(root)
            args = SimpleNamespace(
                root=root,
                poll_seconds=0.0,
                heartbeat_seconds=9999.0,
                managed_gpu_indices="0,1,2,3,4,5,6,7",
                supervisor_grace_seconds=15.0,
                enable_compat_kill_queue=False,
                foreign_gpu_memory_threshold_mb=2048,
                startup_min_schedulable_gpu_count=8,
                startup_unhealthy_action="wait",
                once=True,
            )
            startup_availability = {
                "active_gpu_indices": [],
                "active_untracked_gpu_indices": [],
                "externally_blocked_gpu_indices": ["4", "5", "6", "7"],
                "schedulable_gpu_indices": ["0", "1", "2", "3"],
                "unavailable_gpu_indices": ["4", "5", "6", "7"],
                "free_gpu_indices": ["0", "1", "2", "3"],
                "gpu_external_block_reasons": {"4": ["foreign_proc pid=999 name=[Not Found] mem=45076"]},
            }
            state_payloads: list[dict[str, object]] = []
            event_names: list[str] = []

            def record_state(_layout, payload, *, acquired_at_epoch=None):
                state_payloads.append(payload.copy())

            def record_event(_layout, event, **_kwargs):
                event_names.append(event)

            with (
                mock.patch.object(module, "parse_args", return_value=args),
                mock.patch.object(module, "acquire_controller_lease", return_value=123.0),
                mock.patch.object(module, "release_controller_lease"),
                mock.patch.object(module, "refresh_controller_lease"),
                mock.patch.object(module, "detect_managed_gpu_indices", return_value=["0", "1", "2", "3", "4", "5", "6", "7"]),
                mock.patch.object(module, "adopt_running_jobs", return_value=[]),
                mock.patch.object(module, "probe_gpu_status", return_value=[]),
                mock.patch.object(module, "probe_gpu_processes", return_value=[]),
                mock.patch.object(module, "summarize_gpu_availability", return_value=startup_availability),
                mock.patch.object(module, "harvest_finished_runs", side_effect=lambda _layout, runs: runs),
                mock.patch.object(module, "next_control_request", return_value=None),
                mock.patch.object(module, "process_kill_requests", side_effect=lambda _layout, runs, enabled=False: (runs, False)),
                mock.patch.object(module, "dispatch_launchable_jobs", side_effect=lambda _layout, runs, *_args, **_kwargs: runs),
                mock.patch.object(module, "next_visible_json", return_value=None),
                mock.patch.object(module, "write_controller_state_snapshots", side_effect=record_state),
                mock.patch.object(module, "append_controller_event", side_effect=record_event),
                mock.patch.object(module, "append_jsonl"),
            ):
                rc = module.main()

            self.assertEqual(rc, 0)
            self.assertIn("controller_startup_waiting_for_gpus", event_names)
            startup_states = [payload for payload in state_payloads if payload.get("startup_health_status") == "waiting"]
            self.assertTrue(startup_states)
            self.assertEqual(startup_states[0]["last_control_action"], "startup_waiting_for_gpus")
            self.assertEqual(startup_states[0]["startup_health_action"], "wait")
            self.assertEqual(
                startup_states[0]["startup_health_reason"],
                "schedulable_gpu_count=4 < required=8",
            )

    def test_startup_gpu_shortage_exit_mode_keeps_old_failure(self) -> None:
        module = load_module(CONTROLLER_SCRIPT, "controller_startup_exit_mode_test")
        with tempfile.TemporaryDirectory(prefix="node-controller-startup-exit-") as tmpdir:
            root = Path(tmpdir) / "runtime"
            root.mkdir(parents=True, exist_ok=True)
            layout = module.build_layout(root)
            args = SimpleNamespace(
                root=root,
                poll_seconds=0.0,
                heartbeat_seconds=9999.0,
                managed_gpu_indices="0,1,2,3,4,5,6,7",
                supervisor_grace_seconds=15.0,
                enable_compat_kill_queue=False,
                foreign_gpu_memory_threshold_mb=2048,
                startup_min_schedulable_gpu_count=8,
                startup_unhealthy_action="exit",
                once=True,
            )
            startup_availability = {
                "active_gpu_indices": [],
                "active_untracked_gpu_indices": [],
                "externally_blocked_gpu_indices": ["4", "5", "6", "7"],
                "schedulable_gpu_indices": ["0", "1", "2", "3"],
                "unavailable_gpu_indices": ["4", "5", "6", "7"],
                "free_gpu_indices": ["0", "1", "2", "3"],
                "gpu_external_block_reasons": {"4": ["foreign_proc pid=999 name=[Not Found] mem=45076"]},
            }
            state_payloads: list[dict[str, object]] = []
            event_names: list[str] = []

            def record_state(_layout, payload, *, acquired_at_epoch=None):
                state_payloads.append(payload.copy())

            def record_event(_layout, event, **_kwargs):
                event_names.append(event)

            with (
                mock.patch.object(module, "parse_args", return_value=args),
                mock.patch.object(module, "acquire_controller_lease", return_value=123.0),
                mock.patch.object(module, "release_controller_lease"),
                mock.patch.object(module, "detect_managed_gpu_indices", return_value=["0", "1", "2", "3", "4", "5", "6", "7"]),
                mock.patch.object(module, "adopt_running_jobs", return_value=[]),
                mock.patch.object(module, "probe_gpu_status", return_value=[]),
                mock.patch.object(module, "probe_gpu_processes", return_value=[]),
                mock.patch.object(module, "summarize_gpu_availability", return_value=startup_availability),
                mock.patch.object(module, "write_controller_state_snapshots", side_effect=record_state),
                mock.patch.object(module, "append_controller_event", side_effect=record_event),
                mock.patch.object(module, "append_jsonl"),
            ):
                rc = module.main()

            self.assertEqual(rc, 2)
            self.assertIn("controller_startup_unhealthy", event_names)
            startup_states = [payload for payload in state_payloads if payload.get("startup_health_status") == "failed"]
            self.assertTrue(startup_states)
            self.assertEqual(startup_states[0]["startup_health_action"], "exit")

    def test_status_script_reads_sandbox_pointer_and_flags_untracked_gpu(self) -> None:
        with tempfile.TemporaryDirectory(prefix="node-controller-status-") as tmpdir:
            sandbox_root = Path(tmpdir)
            runtime_root = sandbox_root / "runtime" / "platform_t-20260321-abcde"
            for path in [
                runtime_root / "state",
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

            (runtime_root / "jobs" / "queue" / "queued.json").write_text("{}", encoding="utf-8")
            (runtime_root / "jobs" / "done" / "done.result.json").write_text("{}", encoding="utf-8")
            (runtime_root / "runtime_unused.json").write_text("{}", encoding="utf-8")

            pointer = {
                "runtime_root": str(runtime_root),
                "updated_at_epoch": time.time(),
            }
            (sandbox_root / "runtime").mkdir(parents=True, exist_ok=True)
            (sandbox_root / "runtime" / "current_runtime.json").write_text(
                json.dumps(pointer, indent=2),
                encoding="utf-8",
            )

            controller_state = {
                "updated_at_epoch": time.time(),
                "pid": 2164,
                "hostname": "dev-intern-02",
                "active_jobs": [
                    {
                        "job_id": "train_on_0",
                        "allocated_gpu_indices": ["0"],
                        "pid": 999,
                        "timeout_seconds": 600,
                        "workdir": "/dev_vepfs/rc_wu",
                    }
                ],
                "active_job_count": 1,
                "gpu_status": [
                    {"index": 0, "util_pct": 93, "mem_used_mb": 28000, "mem_total_mb": 81920, "temp_c": 64},
                    {"index": 1, "util_pct": 87, "mem_used_mb": 25000, "mem_total_mb": 81920, "temp_c": 61},
                ],
                "gpu_processes": [
                    {"gpu_index": 0, "pid": 999, "controller_job_id": "train_on_0"},
                    {"gpu_index": 1, "pid": 12345, "controller_job_id": None},
                ],
                "managed_gpu_indices": ["0", "1"],
            }
            (runtime_root / "state" / "controller_state.json").write_text(
                json.dumps(controller_state, indent=2),
                encoding="utf-8",
            )
            (runtime_root / "state" / "controller_lease.json").write_text(
                json.dumps({"updated_at_epoch": time.time()}, indent=2),
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, str(STATUS_SCRIPT), "--sandbox-root", str(sandbox_root)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                cwd=str(REPO_ROOT),
            )

            self.assertIn(str(runtime_root), result.stdout)
            self.assertIn("train_on_0", result.stdout)
            self.assertIn("external_or_untracked", result.stdout)
            self.assertIn("queue=1", result.stdout)

    def test_supervisor_helpers_extract_task_name_and_id(self) -> None:
        module = load_module(SUPERVISOR_SCRIPT, "controller_supervisor_test_helpers")
        yaml_text = 'TaskName: "zoom_dino_volc_dispatcher_proto_20260318_meshovernight_1n"\nPriority: 4\n'
        self.assertEqual(
            module.extract_task_name_from_yaml_text(yaml_text),
            "zoom_dino_volc_dispatcher_proto_20260318_meshovernight_1n",
        )
        submit_text = "submitted task t-20260321023221-92v8s successfully"
        self.assertEqual(module.extract_task_id_from_text(submit_text), "t-20260321023221-92v8s")

    def test_supervisor_dry_run_reports_submit_intent(self) -> None:
        module = load_module(SUPERVISOR_SCRIPT, "controller_supervisor_test_dryrun")
        with tempfile.TemporaryDirectory(prefix="node-controller-supervisor-") as tmpdir:
            sandbox_root = Path(tmpdir)
            submit_config = sandbox_root / "submit.yaml"
            submit_config.parent.mkdir(parents=True, exist_ok=True)
            submit_config.write_text('TaskName: "demo_controller_task"\n', encoding="utf-8")
            args = SimpleNamespace(
                sandbox_root=sandbox_root,
                submit_config=submit_config,
                task_name="",
                volc_binary="volc",
                poll_seconds=1.0,
                healthy_max_age_seconds=30.0,
                dry_run=True,
                once=True,
            )
            with mock.patch.object(module, "list_running_task_ids", return_value=[]):
                state = module.supervise_once(args)
            self.assertEqual(state["action"], "would_submit_new_task")
            self.assertEqual(state["task_name"], "demo_controller_task")
            self.assertFalse(state["controller_healthy"])

    def test_supervisor_syncs_runtime_pointer(self) -> None:
        module = load_module(SUPERVISOR_SCRIPT, "controller_supervisor_test_pointer")
        with tempfile.TemporaryDirectory(prefix="node-controller-pointer-") as tmpdir:
            sandbox_root = Path(tmpdir)
            runtime_root = sandbox_root / "runtime" / "platform_t-20260321-abcde"
            state = {
                "updated_at_epoch": 123.0,
                "runtime_root": str(runtime_root),
                "task_name": "demo_controller_task",
                "action": "healthy_runtime",
                "task_id": "t-20260321-abcde",
            }
            module.sync_current_runtime_pointer(sandbox_root, state)
            pointer_path = sandbox_root / "runtime" / "current_runtime.json"
            self.assertTrue(pointer_path.exists())
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            self.assertEqual(pointer["runtime_root"], str(runtime_root))
            self.assertEqual(pointer["task_id"], "t-20260321-abcde")

            module.sync_current_runtime_pointer(
                sandbox_root,
                {
                    "updated_at_epoch": 124.0,
                    "runtime_root": None,
                    "task_name": "demo_controller_task",
                    "action": "would_submit_new_task",
                    "task_id": None,
                },
            )
            self.assertFalse(pointer_path.exists())


if __name__ == "__main__":
    unittest.main()
