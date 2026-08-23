from __future__ import annotations

import json
import secrets
import socket
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import Any

from .common import ARTIFACT_DIR, PROTOCOL, VERSION, capabilities, sessions_from_cfg, validate_job


class Handler(BaseHTTPRequestHandler):
    server_version = "ChatGPTRelay/1.1"

    def send_json(self, code: int, obj: Any):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        expected = str(self.server.cfg.get("local_api_token") or "")
        return bool(expected) and secrets.compare_digest(self.headers.get("X-Relay-Token", ""), expected)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            transport = self.server.transport.status_snapshot()
            age = transport.get("queue_last_ok_age_s")
            max_age = max(45, int(self.server.cfg.get("poll_seconds", 3)) * 5 + 15)
            ok = (
                age is not None
                and age < max_age
                and int(transport.get("consecutive_api_errors") or 0) < 3
                and not transport.get("stale_claims")
            )
            return self.send_json(
                200 if ok else 503,
                {
                    "ok": ok,
                    "protocol": PROTOCOL,
                    "relay_version": VERSION,
                    "host": socket.gethostname(),
                    "active": self.server.runner.snapshot(),
                    "sessions": self.server.sessions.snap(),
                    "transport": transport,
                },
            )
        if not self.authorized():
            return self.send_json(401, {"error": "unauthorized"})
        if parsed.path == "/capabilities":
            return self.send_json(200, capabilities(self.server.cfg))
        if parsed.path.startswith("/artifact/"):
            rel = urllib.parse.unquote(parsed.path[len("/artifact/"):])
            path = (ARTIFACT_DIR / rel).resolve()
            root = ARTIFACT_DIR.resolve()
            if path != root and root not in path.parents:
                return self.send_json(403, {"error": "bad path"})
            if not path.is_file():
                return self.send_json(404, {"error": "not found"})
            size = path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return
        return self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self.authorized():
            return self.send_json(401, {"error": "unauthorized"})
        length = min(int(self.headers.get("Content-Length", "0")), 2_000_000)
        raw = self.rfile.read(length)
        try:
            job = json.loads(raw)
            job.setdefault("protocol", PROTOCOL)
            job.setdefault("session", sessions_from_cfg(self.server.cfg)[0])
            job.setdefault("job_id", f"http-{int(time.time() * 1000)}")
            validate_job(self.server.cfg, job, enforce_freshness=False)
            result = self.server.runner.execute(job)
            return self.send_json(200, {"job_id": job["job_id"], "status": "ok", "result": result})
        except Exception as exc:
            return self.send_json(400, {"status": "error", "error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, *_args):
        pass
