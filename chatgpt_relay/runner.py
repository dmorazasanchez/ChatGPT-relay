from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import common

ARTIFACT_DIR = common.ARTIFACT_DIR
CONTROL_STATE = common.CONTROL_STATE


class BoundedCapture:
    def __init__(self, limit: int = 28000):
        self.limit = max(1024, int(limit))
        self.head_limit = self.limit // 2
        self.tail_limit = self.limit - self.head_limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self.lock = threading.Lock()

    def feed(self, chunk: bytes):
        if not chunk:
            return
        with self.lock:
            self.total += len(chunk)
            view = chunk
            if len(self.head) < self.head_limit:
                take = min(self.head_limit - len(self.head), len(view))
                self.head.extend(view[:take])
                view = view[take:]
            if view:
                self.tail.extend(view)
                if len(self.tail) > self.tail_limit:
                    del self.tail[:-self.tail_limit]

    def text(self) -> tuple[str, bool]:
        with self.lock:
            truncated = self.total > self.limit
            if truncated:
                raw = bytes(self.head) + b"\n...[truncated; full output saved locally]...\n" + bytes(self.tail)
            else:
                raw = bytes(self.head) + bytes(self.tail)
        return raw.decode("utf-8", errors="replace"), truncated

    def size(self) -> int:
        with self.lock:
            return self.total


def artifact_path(session: str, job_id: str, stream: str) -> Path:
    if stream not in common.ARTIFACT_STREAMS:
        raise ValueError(f"invalid artifact stream {stream!r}")
    directory = ARTIFACT_DIR / f"{session}--{job_id}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{stream}.log"


def _artifact_existing_path(session: str, job_id: str, stream: str) -> Path:
    path = artifact_path(session, job_id, stream)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return path


def artifact_info(session: str, job_id: str, stream: str) -> dict:
    path = _artifact_existing_path(session, job_id, stream)
    st = path.stat()
    return {
        "session": session, "job_id": job_id, "stream": stream,
        "size_bytes": st.st_size, "modified_unix": int(st.st_mtime), "local_path": str(path),
    }


def artifact_read(session: str, job_id: str, stream: str, offset=0, max_bytes=65536) -> dict:
    path = _artifact_existing_path(session, job_id, stream)
    offset = max(0, int(offset))
    max_bytes = max(1, min(int(max_bytes), 262144))
    size = path.stat().st_size
    if offset > size:
        offset = size
    with path.open("rb") as handle:
        handle.seek(offset)
        raw = handle.read(max_bytes)
    next_offset = offset + len(raw)
    return {
        "session": session, "job_id": job_id, "stream": stream, "offset": offset,
        "next_offset": next_offset, "size_bytes": size, "eof": next_offset >= size,
        "content": raw.decode("utf-8", errors="replace"),
    }


def artifact_tail(session: str, job_id: str, stream: str, lines=200, max_bytes=262144) -> dict:
    path = _artifact_existing_path(session, job_id, stream)
    lines = max(1, min(int(lines), 2000))
    max_bytes = max(1024, min(int(max_bytes), 1_048_576))
    size = path.stat().st_size
    with path.open("rb") as handle:
        pos, total, newline_count = size, 0, 0
        chunks: list[bytes] = []
        while pos > 0 and total < max_bytes and newline_count <= lines:
            take = min(65536, pos, max_bytes - total)
            pos -= take
            handle.seek(pos)
            chunk = handle.read(take)
            chunks.append(chunk)
            total += len(chunk)
            newline_count += chunk.count(b"\n")
    raw = b"".join(reversed(chunks))
    tail_lines = raw.decode("utf-8", errors="replace").splitlines()[-lines:]
    return {
        "session": session, "job_id": job_id, "stream": stream, "lines": lines,
        "size_bytes": size, "read_bytes": len(raw), "truncated_before": pos > 0,
        "content": "\n".join(tail_lines),
    }


class Sessions:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.names = common.sessions_from_cfg(cfg)
        old = common.read_json(CONTROL_STATE, {}) or {}
        self.lock = threading.Lock()
        self.data = {}
        now = int(time.time())
        for session in self.names:
            item = old.get(session, {}) if isinstance(old, dict) else {}
            self.data[session] = {
                "mode": item.get("mode", "running"), "priority": item.get("priority", ""),
                "messages": list(item.get("messages", []))[-30:], "updated": int(item.get("updated", now)),
            }

    def snap(self, session: str | None = None):
        with self.lock:
            obj = self.data[session] if session else self.data
            return json.loads(json.dumps(obj))

    def save(self):
        common.write_json(CONTROL_STATE, self.snap())

    def allowed(self, session: str):
        mode = self.snap(session)["mode"]
        return mode == "running", "" if mode == "running" else f"session-{mode}"

    def apply(self, session: str, action: str, text: str = "", source="github-control"):
        action = str(action).upper()
        now = int(time.time())
        with self.lock:
            item = self.data[session]
            if action == "PAUSE": item["mode"] = "paused"
            elif action == "RESUME": item["mode"] = "running"
            elif action == "STOP": item["mode"] = "stopped"
            elif action == "NOTE":
                if not text.strip(): raise ValueError("NOTE requires text")
            elif action == "PRIORITY":
                if not text.strip(): raise ValueError("PRIORITY requires text")
                item["priority"] = text.strip()
            elif action == "CLEAR_PRIORITY": item["priority"] = ""
            else: raise ValueError(f"unsupported control action {action!r}")
            message = {
                "id": f"{session}-{now}-{os.urandom(3).hex()}", "action": action,
                "text": text.strip(), "source": source, "unix": now,
            }
            item["messages"].append(message)
            item["messages"] = item["messages"][-30:]
            item["updated"] = now
        self.save()
        return message


class Runner:
    def __init__(self, cfg: dict, sessions: Sessions):
        self.cfg = cfg
        self.sessions = sessions
        self.active: dict[str, dict] = {}
        self.lock = threading.Lock()

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            return [{
                "key": key, "job_id": info["job_id"], "session": info["session"],
                "started_unix": info["unix"], "elapsed_s": round(now - info["mono"], 1),
                "command": info.get("command", "")[:180],
                "source_blob_sha": info.get("source_blob_sha"),
                "stdout_bytes": info.get("stdout_capture").size() if info.get("stdout_capture") else None,
                "stderr_bytes": info.get("stderr_capture").size() if info.get("stderr_capture") else None,
            } for key, info in self.active.items()]

    def cancel(self, session: str, job_id: str, expected_source_blob_sha: str | None = None):
        key = f"{session}--{job_id}"
        with self.lock:
            info = self.active.get(key)
        if not info:
            return {"cancelled": False, "reason": "not-running"}
        active_sha = str(info.get("source_blob_sha") or "")
        if expected_source_blob_sha and active_sha != expected_source_blob_sha:
            return {
                "cancelled": False, "reason": "source-blob-mismatch", "session": session, "job_id": job_id,
                "expected_source_blob_sha": expected_source_blob_sha,
                "active_source_blob_sha": active_sha or None,
            }
        try:
            os.killpg(info["process"].pid, signal.SIGTERM)
            return {
                "cancelled": True, "session": session, "job_id": job_id,
                "source_blob_sha": active_sha or None,
            }
        except Exception as exc:
            return {"cancelled": False, "reason": str(exc)}

    def cancel_session(self, session: str):
        with self.lock:
            ids = [x["job_id"] for x in self.active.values() if x["session"] == session]
        return {job_id: self.cancel(session, job_id) for job_id in ids}

    @staticmethod
    def _pump(pipe, target: Path, capture: BoundedCapture):
        try:
            with target.open("wb") as handle:
                while True:
                    chunk = pipe.read(65536)
                    if not chunk: break
                    handle.write(chunk)
                    capture.feed(chunk)
        finally:
            try: pipe.close()
            except Exception: pass

    def shell(self, job: dict):
        session = common.normalize_session(self.cfg, job.get("session"))
        job_id = common.normalize_job_id(session, job.get("job_id"))
        cwd = common.ensure_allowed(str(job["cwd"]), self.cfg, exists=True, directory=True)
        if job.get("command_b64"):
            try:
                command = base64.b64decode(str(job["command_b64"]).encode(), validate=True).decode("utf-8")
            except Exception as exc:
                raise ValueError(f"invalid command_b64: {exc}") from exc
        else:
            command = str(job["command"])
        timeout = max(1, min(int(job.get("timeout", 120)), int(self.cfg.get("max_timeout", 1800))))
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (job.get("env") or {}).items()})
        stdout_path, stderr_path = artifact_path(session, job_id, "stdout"), artifact_path(session, job_id, "stderr")
        stdout_capture = BoundedCapture(int(self.cfg.get("result_output_limit", 28000)))
        stderr_capture = BoundedCapture(int(self.cfg.get("result_output_limit", 28000)))
        started = time.monotonic()
        proc = subprocess.Popen(
            [self.cfg.get("shell", "/bin/bash"), "-lc", command], cwd=str(cwd), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        assert proc.stdout is not None and proc.stderr is not None
        out_thread = threading.Thread(target=self._pump, args=(proc.stdout, stdout_path, stdout_capture), daemon=True)
        err_thread = threading.Thread(target=self._pump, args=(proc.stderr, stderr_path, stderr_capture), daemon=True)
        out_thread.start(); err_thread.start()
        key = f"{session}--{job_id}"
        with self.lock:
            self.active[key] = {
                "process": proc, "session": session, "job_id": job_id, "mono": started,
                "unix": int(time.time()), "command": command,
                "source_blob_sha": str(job.get("_source_blob_sha") or "") or None,
                "stdout_capture": stdout_capture, "stderr_capture": stderr_capture,
            }
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try: os.killpg(proc.pid, signal.SIGTERM)
            except Exception: pass
            try: proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except Exception: pass
                proc.wait()
        finally:
            out_thread.join(timeout=10); err_thread.join(timeout=10)
            with self.lock: self.active.pop(key, None)
        stdout, stdout_truncated = stdout_capture.text()
        stderr, stderr_truncated = stderr_capture.text()
        return {
            "exit_code": 124 if timed_out else proc.returncode, "timed_out": timed_out,
            "duration_s": round(time.monotonic() - started, 3), "cwd": str(cwd),
            "stdout": stdout, "stderr": stderr, "stdout_truncated": stdout_truncated, "stderr_truncated": stderr_truncated,
            "stdout_bytes": stdout_capture.size(), "stderr_bytes": stderr_capture.size(),
            "stdout_artifact": str(stdout_path), "stderr_artifact": str(stderr_path),
            "stdout_artifact_ref": {"session": session, "job_id": job_id, "stream": "stdout"},
            "stderr_artifact_ref": {"session": session, "job_id": job_id, "stream": "stderr"},
        }

    def execute(self, job: dict):
        session = common.normalize_session(self.cfg, job.get("session"))
        job["session"] = session
        op = str(job.get("op") or "")
        if op == "ping": return {"pong": True, "host": socket.gethostname(), "time": int(time.time()), "session": session}
        if op == "shell": return self.shell(job)
        if op == "cancel":
            target_session = common.normalize_session(self.cfg, job.get("target_session") or session)
            target_job = common.normalize_job_id(target_session, job.get("target_job_id"))
            expected_sha = str(job.get("target_source_blob_sha") or "") or None
            return self.cancel(target_session, target_job, expected_sha)
        if op == "control_status": return self.sessions.snap(session)
        if op == "read_file":
            path = common.ensure_allowed(str(job["path"]), self.cfg, exists=True)
            text = path.read_text(encoding="utf-8", errors="replace")
            start = max(1, int(job.get("start_line", 1))); end = int(job.get("end_line", start + 399))
            lines = text.splitlines(); end = min(len(lines), end)
            return {"path": str(path), "content": "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))}
        if op == "write_file":
            path = common.ensure_allowed(str(job["path"]), self.cfg)
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text(str(job.get("content", "")), encoding="utf-8")
            return {"path": str(path), "bytes": path.stat().st_size}
        if op == "git_status":
            cwd = common.ensure_allowed(str(job["cwd"]), self.cfg, exists=True, directory=True)
            proc = common.run(["git", "-C", str(cwd), "status", "--short", "--branch"], timeout=60)
            return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
        if op == "git_diff":
            cwd = common.ensure_allowed(str(job["cwd"]), self.cfg, exists=True, directory=True)
            args = ["git", "-C", str(cwd), "diff", "--no-ext-diff", "--unified=3"]
            if job.get("cached"): args.insert(4, "--cached")
            proc = common.run(args, timeout=60); stdout, truncated = common.clip(proc.stdout or "")
            return {"exit_code": proc.returncode, "stdout": stdout, "stdout_truncated": truncated, "stderr": proc.stderr}
        if op == "list_files":
            root = common.ensure_allowed(str(job["path"]), self.cfg, exists=True, directory=True)
            limit, depth, output, base_parts = min(int(job.get("max_entries", 500)), 3000), min(int(job.get("max_depth", 3)), 8), [], len(root.parts)
            for current, dirs, files in os.walk(root):
                current_path = Path(current)
                if len(current_path.parts) - base_parts >= depth: dirs[:] = []
                dirs[:] = [d for d in dirs if d not in {".git", ".cache", "node_modules"}]
                for name in sorted(dirs): output.append(str((current_path / name).relative_to(root)) + "/")
                for name in sorted(files): output.append(str((current_path / name).relative_to(root)))
                if len(output) >= limit: return {"entries": output[:limit], "truncated": True}
            return {"entries": output, "truncated": False}
        if op in {"artifact_info", "artifact_read", "artifact_tail"}:
            target_session = common.normalize_session(self.cfg, job.get("target_session") or session)
            target_job = common.normalize_job_id(target_session, job.get("target_job_id")); stream = str(job.get("stream"))
            if stream not in common.ARTIFACT_STREAMS: raise ValueError(f"stream must be one of {common.ARTIFACT_STREAMS}")
            if op == "artifact_info": return artifact_info(target_session, target_job, stream)
            if op == "artifact_read": return artifact_read(target_session, target_job, stream, job.get("offset", 0), job.get("max_bytes", 65536))
            return artifact_tail(target_session, target_job, stream, job.get("lines", 200))
        raise ValueError(f"unsupported op {op!r}")
