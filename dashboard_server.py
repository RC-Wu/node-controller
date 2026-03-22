#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import socket
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from runtime_io import build_runtime_bundle, list_job_rows, resolve_runtime_root, tail_jsonl, tail_text, write_json_atomic


STATIC_ROOT = Path(__file__).resolve().parent / "dashboard" / "static"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Serve the node-controller operator dashboard.")
    ap.add_argument("--root", type=Path, default=Path("."), help="Controller sandbox root or runtime root.")
    ap.add_argument("--sandbox-root", type=Path, dest="sandbox_root", help="Alias for --root when targeting a sandbox.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--allow-write-actions", action="store_true", help="Enable POST control actions from the dashboard.")
    ap.add_argument("--default-history-tail", type=int, default=300)
    ap.add_argument("--default-log-tail", type=int, default=400)
    return ap.parse_args()


class DashboardService:
    def __init__(
        self,
        *,
        root: Path,
        allow_write_actions: bool,
        default_history_tail: int,
        default_log_tail: int,
    ) -> None:
        self.root = root
        self.allow_write_actions = allow_write_actions
        self.default_history_tail = default_history_tail
        self.default_log_tail = default_log_tail

    def runtime_root(self) -> Path:
        return resolve_runtime_root(self.root)

    def request_metadata(self, *, remote_addr: str, source: str) -> dict:
        return {
            "requested_by": os.environ.get("USER") or os.environ.get("USERNAME") or "dashboard",
            "requested_from_host": socket.gethostname(),
            "requested_from_pid": os.getpid(),
            "request_source": source,
            "requested_from_remote_addr": remote_addr,
            "requested_at_epoch": time.time(),
        }

    def summary(self) -> dict:
        runtime_root = self.runtime_root()
        bundle = build_runtime_bundle(self.root)
        bundle["allow_write_actions"] = self.allow_write_actions
        bundle["recent_controller_events"] = tail_jsonl(runtime_root / "logs" / "controller_events.jsonl", 80)
        bundle["recent_admin_audit"] = tail_jsonl(runtime_root / "logs" / "controller_admin_audit.jsonl", 80)
        bundle["runtime_root_resolved_at_epoch"] = time.time()
        return bundle

    def history(self, *, tail: int) -> dict:
        runtime_root = self.runtime_root()
        return {
            "runtime_root": str(runtime_root),
            "heartbeat": tail_jsonl(runtime_root / "logs" / "controller_heartbeat.jsonl", tail),
            "controller_events": tail_jsonl(runtime_root / "logs" / "controller_events.jsonl", tail),
            "admin_audit": tail_jsonl(runtime_root / "logs" / "controller_admin_audit.jsonl", tail),
        }

    def jobs(self) -> dict:
        runtime_root = self.runtime_root()
        return {
            "runtime_root": str(runtime_root),
            "jobs": list_job_rows(runtime_root, limit_per_bucket=250),
        }

    def job_log(self, job_id: str, *, tail: int) -> dict:
        runtime_root = self.runtime_root()
        safe_job_id = "".join(ch for ch in job_id if ch not in "/\\")
        return {
            "runtime_root": str(runtime_root),
            "job_id": safe_job_id,
            "log_lines": tail_text(runtime_root / "logs" / "jobs" / f"{safe_job_id}.log", tail),
            "event_rows": tail_jsonl(runtime_root / "logs" / "jobs" / f"{safe_job_id}.events.jsonl", tail),
        }

    def enqueue_control(self, payload: dict) -> Path:
        runtime_root = self.runtime_root()
        request_id = str(payload.get("request_id") or f"{payload.get('action', 'request')}_{time.strftime('%Y%m%dT%H%M%S')}")
        payload["request_id"] = request_id
        control_queue = runtime_root / "control" / "queue"
        control_queue.mkdir(parents=True, exist_ok=True)
        out = control_queue / f"{request_id}.json"
        write_json_atomic(out, payload)
        return out

    def kill_job(self, job_id: str, *, remote_addr: str) -> dict:
        payload = {
            "schema_version": 2,
            "action": "cancel_active_job",
            "reason": f"dashboard kill {job_id}",
            "signal": "TERM",
            "grace_seconds": 15.0,
            "job_ids": [job_id],
            "purge_queue": False,
            "cancel_active_job": False,
            "submitted_at_epoch": time.time(),
            **self.request_metadata(remote_addr=remote_addr, source="dashboard_http_kill"),
        }
        out = self.enqueue_control(payload)
        return {"request_path": str(out), "request": payload}

    def control(self, payload: dict, *, remote_addr: str) -> dict:
        body = dict(payload)
        body.setdefault("schema_version", 2)
        body.setdefault("submitted_at_epoch", time.time())
        body.update(self.request_metadata(remote_addr=remote_addr, source="dashboard_http_control"))
        out = self.enqueue_control(body)
        return {"request_path": str(out), "request": body}


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: BaseHTTPRequestHandler, status: int, payload: str) -> None:
    body = payload.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(service: DashboardService):
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "NodeControllerDashboard/1.0"

        def log_message(self, format: str, *args) -> None:
            return

        def _serve_static(self, path: str) -> None:
            rel = path.lstrip("/") or "index.html"
            file_path = (STATIC_ROOT / rel).resolve()
            if STATIC_ROOT.resolve() not in file_path.parents and file_path != STATIC_ROOT.resolve():
                text_response(self, HTTPStatus.FORBIDDEN, "forbidden")
                return
            if file_path.is_dir():
                file_path = file_path / "index.html"
            if not file_path.exists():
                text_response(self, HTTPStatus.NOT_FOUND, "not found")
                return
            body = file_path.read_bytes()
            content_type, _ = mimetypes.guess_type(str(file_path))
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{content_type or 'application/octet-stream'}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == "/api/summary":
                    json_response(self, HTTPStatus.OK, service.summary())
                    return
                if parsed.path == "/api/history":
                    tail = int(query.get("tail", [str(service.default_history_tail)])[0])
                    json_response(self, HTTPStatus.OK, service.history(tail=max(1, min(tail, 2000))))
                    return
                if parsed.path == "/api/jobs":
                    json_response(self, HTTPStatus.OK, service.jobs())
                    return
                if parsed.path.startswith("/api/logs/job/"):
                    job_id = unquote(parsed.path[len("/api/logs/job/") :])
                    tail = int(query.get("tail", [str(service.default_log_tail)])[0])
                    json_response(self, HTTPStatus.OK, service.job_log(job_id, tail=max(1, min(tail, 2000))))
                    return
                if parsed.path == "/api/health":
                    json_response(self, HTTPStatus.OK, {"ok": True, "time": time.time()})
                    return
                if parsed.path == "/" or parsed.path.startswith("/assets/") or parsed.path.endswith((".js", ".css", ".html")):
                    self._serve_static("index.html" if parsed.path == "/" else parsed.path)
                    return
                self._serve_static(parsed.path)
            except Exception as exc:
                json_response(
                    self,
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": type(exc).__name__, "message": str(exc)},
                )

        def do_POST(self) -> None:
            if not service.allow_write_actions:
                json_response(self, HTTPStatus.FORBIDDEN, {"error": "write_actions_disabled"})
                return
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                json_response(self, HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return
            try:
                if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/kill"):
                    job_id = unquote(parsed.path[len("/api/jobs/") : -len("/kill")]).strip("/")
                    json_response(
                        self,
                        HTTPStatus.OK,
                        service.kill_job(job_id, remote_addr=self.client_address[0]),
                    )
                    return
                if parsed.path == "/api/control":
                    json_response(
                        self,
                        HTTPStatus.OK,
                        service.control(payload if isinstance(payload, dict) else {}, remote_addr=self.client_address[0]),
                    )
                    return
                json_response(self, HTTPStatus.NOT_FOUND, {"error": "not_found"})
            except Exception as exc:
                json_response(
                    self,
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": type(exc).__name__, "message": str(exc)},
                )

    return DashboardHandler


def create_server(args: argparse.Namespace) -> ThreadingHTTPServer:
    root = (args.sandbox_root or args.root).resolve()
    service = DashboardService(
        root=root,
        allow_write_actions=args.allow_write_actions,
        default_history_tail=args.default_history_tail,
        default_log_tail=args.default_log_tail,
    )
    return ThreadingHTTPServer((args.host, args.port), make_handler(service))


def main() -> int:
    args = parse_args()
    httpd = create_server(args)
    host, port = httpd.server_address[:2]
    print(f"http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        httpd.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
