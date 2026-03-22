#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any


TASK_ID_PATTERN = re.compile(r"\b(t-[0-9]{8,}-[a-z0-9]+)\b", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Conservative host-side supervisor for the long-lived controller task.")
    ap.add_argument("--sandbox-root", type=Path, required=True, help="Controller sandbox root on dev-intern-02.")
    ap.add_argument("--submit-config", type=Path, required=True, help="Volc submit YAML to use when a new task must be launched.")
    ap.add_argument("--task-name", default="", help="Optional TaskName override. If omitted, parse it from the submit YAML.")
    ap.add_argument("--volc-binary", default="volc")
    ap.add_argument("--poll-seconds", type=float, default=30.0)
    ap.add_argument("--healthy-max-age-seconds", type=float, default=45.0)
    ap.add_argument("--dry-run", action="store_true", help="Report intended actions without submitting a new task.")
    ap.add_argument("--once", action="store_true", help="Run one supervision pass and exit.")
    return ap.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def try_read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = read_json(path)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def extract_task_name_from_yaml_text(text: str) -> str:
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() != "TaskName":
            continue
        return value.strip().strip('"').strip("'")
    return ""


def extract_task_id_from_text(text: str) -> str:
    matches = TASK_ID_PATTERN.findall(text)
    return matches[-1] if matches else ""


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def list_running_task_ids(volc_binary: str, task_name: str) -> list[str]:
    if not task_name:
        return []
    result = run_command([volc_binary, "ml_task", "list", "-s", "Running", "-n", task_name, "-o", "json"])
    if result.returncode != 0:
        return []
    task_ids = TASK_ID_PATTERN.findall(result.stdout)
    return sorted(set(task_ids))


def is_controller_healthy(runtime_root: Path, max_age_seconds: float) -> tuple[bool, dict[str, Any] | None]:
    state = try_read_json(runtime_root / "state" / "controller_state.json")
    if state is None:
        return False, None
    updated = float(state.get("updated_at_epoch") or 0.0)
    if updated <= 0:
        return False, state
    return (time.time() - updated) <= max(1.0, max_age_seconds), state


def runtime_root_for_task(sandbox_root: Path, task_id: str) -> Path:
    return sandbox_root / "runtime" / f"platform_{task_id}"


def supervisor_state_path(sandbox_root: Path) -> Path:
    return sandbox_root / "runtime" / "supervisor_state.json"


def current_runtime_pointer_path(sandbox_root: Path) -> Path:
    return sandbox_root / "runtime" / "current_runtime.json"


def sync_current_runtime_pointer(sandbox_root: Path, state: dict[str, Any]) -> None:
    pointer_path = current_runtime_pointer_path(sandbox_root)
    runtime_root = state.get("runtime_root")
    if runtime_root:
        write_json_atomic(
            pointer_path,
            {
                "updated_at_epoch": state.get("updated_at_epoch"),
                "runtime_root": runtime_root,
                "task_name": state.get("task_name"),
                "action": state.get("action"),
                "task_id": state.get("task_id"),
            },
        )
        return
    if pointer_path.exists():
        pointer_path.unlink()


def build_state(
    *,
    sandbox_root: Path,
    submit_config: Path,
    task_name: str,
    action: str,
    runtime_root: Path | None,
    running_task_ids: list[str],
    controller_healthy: bool,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "updated_at_epoch": time.time(),
        "sandbox_root": str(sandbox_root),
        "submit_config": str(submit_config),
        "task_name": task_name,
        "action": action,
        "running_task_ids": running_task_ids,
        "controller_healthy": controller_healthy,
        "runtime_root": None if runtime_root is None else str(runtime_root),
        "current_runtime_pointer_path": str(current_runtime_pointer_path(sandbox_root)),
    }
    if details:
        payload.update(details)
    return payload


def supervise_once(args: argparse.Namespace) -> dict[str, Any]:
    sandbox_root = args.sandbox_root.resolve()
    submit_config = args.submit_config.resolve()
    task_name = args.task_name or extract_task_name_from_yaml_text(submit_config.read_text(encoding="utf-8"))
    pointer = try_read_json(current_runtime_pointer_path(sandbox_root))
    current_runtime_root = None if pointer is None else Path(str(pointer.get("runtime_root") or "")).resolve()

    if current_runtime_root is not None:
        healthy, state = is_controller_healthy(current_runtime_root, args.healthy_max_age_seconds)
        if healthy:
            return build_state(
                sandbox_root=sandbox_root,
                submit_config=submit_config,
                task_name=task_name,
                action="healthy_runtime",
                runtime_root=current_runtime_root,
                running_task_ids=list_running_task_ids(args.volc_binary, task_name),
                controller_healthy=True,
                details={"controller_state_age_seconds": round(time.time() - float(state["updated_at_epoch"]), 3)},
            )

    running_task_ids = list_running_task_ids(args.volc_binary, task_name)
    if running_task_ids:
        latest = running_task_ids[-1]
        runtime_root = runtime_root_for_task(sandbox_root, latest)
        healthy, state = is_controller_healthy(runtime_root, args.healthy_max_age_seconds)
        action = "rediscovered_running_task" if healthy else "running_task_without_fresh_heartbeat"
        details = {
            "task_id": latest,
            "controller_state_age_seconds": None if state is None else round(time.time() - float(state.get("updated_at_epoch") or 0.0), 3),
        }
        return build_state(
            sandbox_root=sandbox_root,
            submit_config=submit_config,
            task_name=task_name,
            action=action,
            runtime_root=runtime_root,
            running_task_ids=running_task_ids,
            controller_healthy=healthy,
            details=details,
        )

    command = [args.volc_binary, "ml_task", "submit", "-c", str(submit_config)]
    if args.dry_run:
        return build_state(
            sandbox_root=sandbox_root,
            submit_config=submit_config,
            task_name=task_name,
            action="would_submit_new_task",
            runtime_root=None,
            running_task_ids=[],
            controller_healthy=False,
            details={"command": command},
        )

    result = run_command(command)
    submit_output = result.stdout + ("\n" + result.stderr if result.stderr else "")
    task_id = extract_task_id_from_text(submit_output)
    if not task_id and task_name:
        time.sleep(3.0)
        refreshed = list_running_task_ids(args.volc_binary, task_name)
        if refreshed:
            running_task_ids = refreshed
            task_id = refreshed[-1]
    details = {
        "command": command,
        "submit_returncode": result.returncode,
        "submit_stdout": result.stdout,
        "submit_stderr": result.stderr,
        "task_id": task_id or None,
    }
    return build_state(
        sandbox_root=sandbox_root,
        submit_config=submit_config,
        task_name=task_name,
        action="submitted_new_task" if result.returncode == 0 else "submit_failed",
        runtime_root=None if not task_id else runtime_root_for_task(sandbox_root, task_id),
        running_task_ids=running_task_ids,
        controller_healthy=False,
        details=details,
    )


def main() -> int:
    args = parse_args()
    sandbox_root = args.sandbox_root.resolve()
    while True:
        state = supervise_once(args)
        write_json_atomic(supervisor_state_path(sandbox_root), state)
        sync_current_runtime_pointer(sandbox_root, state)
        if args.once:
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0 if state.get("action") not in {"submit_failed"} else 1
        time.sleep(max(1.0, args.poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
