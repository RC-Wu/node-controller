#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import subprocess
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = REPO_ROOT / "dashboard" / "static"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Local PC proxy for the remote node-controller dashboard.")
    ap.add_argument("--ssh-target", default="dev-intern-02")
    ap.add_argument(
        "--remote-module-root",
        default=None,
        help="Remote directory that contains dashboard_server.py and runtime_io.py. Defaults to --remote-sandbox-root.",
    )
    ap.add_argument(
        "--remote-sandbox-root",
        default="/dev_vepfs/rc_wu/zoom-in-render-dino-classfier/sandboxes/20260318_volc_dispatcher_proto",
        help="Remote controller sandbox root on dev-intern-02.",
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8794)
    ap.add_argument("--default-history-tail", type=int, default=300)
    ap.add_argument("--default-log-tail", type=int, default=400)
    ap.add_argument("--cache-ttl-seconds", type=float, default=2.0)
    return ap.parse_args()


class ProxyService:
    def __init__(
        self,
        *,
        ssh_target: str,
        remote_module_root: str,
        remote_sandbox_root: str,
        default_history_tail: int,
        default_log_tail: int,
        cache_ttl_seconds: float,
    ) -> None:
        self.ssh_target = ssh_target
        self.remote_module_root = remote_module_root
        self.remote_sandbox_root = remote_sandbox_root
        self.default_history_tail = default_history_tail
        self.default_log_tail = default_log_tail
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[str, tuple[float, dict]] = {}

    def _remote_script(self, payload: dict) -> str:
        payload_json = json.dumps(payload, ensure_ascii=False)
        return f"""
from pathlib import Path
import json
import sys

cfg = json.loads({payload_json!r})
sys.path.insert(0, cfg["remote_module_root"])
from dashboard_server import DashboardService

service = DashboardService(
    root=Path(cfg["remote_sandbox_root"]),
    allow_write_actions=False,
    default_history_tail=int(cfg["default_history_tail"]),
    default_log_tail=int(cfg["default_log_tail"]),
)

action = cfg["action"]
if action == "summary":
    result = service.summary()
elif action == "history":
    result = service.history(tail=int(cfg["tail"]))
elif action == "jobs":
    result = service.jobs()
elif action == "job_log":
    result = service.job_log(cfg["job_id"], tail=int(cfg["tail"]))
else:
    raise SystemExit(f"unsupported action: {{action}}")

sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
"""

    def _remote_call(self, action: str, **kwargs) -> dict:
        payload = {
            "action": action,
            "remote_module_root": self.remote_module_root,
            "remote_sandbox_root": self.remote_sandbox_root,
            "default_history_tail": self.default_history_tail,
            "default_log_tail": self.default_log_tail,
            **kwargs,
        }
        cache_key = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        now = time.time()
        if action in {"summary", "history", "jobs"}:
            cached = self._cache.get(cache_key)
            if cached is not None and now - cached[0] <= self.cache_ttl_seconds:
                return cached[1]
        result = subprocess.run(
            ["ssh", self.ssh_target, "python3 -"],
            input=self._remote_script(payload).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=45,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"remote ssh failed ({result.returncode}): {stderr.strip()}")
        try:
            response = json.loads(result.stdout.decode("utf-8"))
        except Exception as exc:
            preview = result.stdout[:400].decode("utf-8", errors="replace")
            raise RuntimeError(f"remote JSON decode failed: {type(exc).__name__}: {exc}; preview={preview!r}") from exc
        if action in {"summary", "history", "jobs"}:
            self._cache[cache_key] = (now, response)
        return response

    def summary(self) -> dict:
        return self._remote_call("summary")

    def history(self, *, tail: int) -> dict:
        return self._remote_call("history", tail=tail)

    def jobs(self) -> dict:
        return self._remote_call("jobs")

    def job_log(self, job_id: str, *, tail: int) -> dict:
        return self._remote_call("job_log", job_id=job_id, tail=tail)


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: BaseHTTPRequestHandler, status: int, payload: str) -> None:
    body = payload.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(service: ProxyService):
    class ProxyHandler(BaseHTTPRequestHandler):
        server_version = "NodeControllerPCProxy/1.0"

        def log_message(self, format: str, *args) -> None:
            return

        def _serve_static(self, path: str) -> None:
            rel = path.lstrip("/") or "index.html"
            rel = rel.split("?", 1)[0]
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
            content_type = content_type or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {
                "application/javascript",
                "image/svg+xml",
            }:
                content_type = f"{content_type}; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
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
                    json_response(
                        self,
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "proxy": "pc_remote_dashboard_proxy",
                            "ssh_target": service.ssh_target,
                            "remote_sandbox_root": service.remote_sandbox_root,
                            "time": time.time(),
                        },
                    )
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
            json_response(self, HTTPStatus.FORBIDDEN, {"error": "write_actions_disabled_in_pc_proxy"})

    return ProxyHandler


def main() -> int:
    args = parse_args()
    remote_module_root = args.remote_module_root or args.remote_sandbox_root
    service = ProxyService(
        ssh_target=args.ssh_target,
        remote_module_root=remote_module_root,
        remote_sandbox_root=args.remote_sandbox_root,
        default_history_tail=args.default_history_tail,
        default_log_tail=args.default_log_tail,
        cache_ttl_seconds=args.cache_ttl_seconds,
    )
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(service))
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
