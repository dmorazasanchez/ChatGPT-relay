#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

APP = "chatgpt-relay"
PROTOCOL = "CHATGPT_RELAY_V1"
VERSION = "1.0.0"
HOME = Path.home()
CFG_PATH = HOME / ".config" / APP / "config.json"
DATA_DIR = HOME / ".local" / "share" / APP
STATE_DIR = DATA_DIR / "job-state"
ARTIFACT_DIR = DATA_DIR / "artifacts"
CONTROL_STATE = DATA_DIR / "control-state.json"
HISTORY = DATA_DIR / "history.jsonl"
INSTANCE = f"{os.getpid()}-{secrets.token_hex(5)}"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, obj: Any, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)


def run(args: list[str], *, input_text: str | None = None, timeout: int = 30):
    return subprocess.run(
        args,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def retry(fn, *, attempts=3, base_delay=0.5):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(base_delay * (2**i))
    raise RuntimeError(str(last)) from last


def _gh_error(proc: subprocess.CompletedProcess[str], what: str):
    detail = (proc.stderr or proc.stdout or "").strip()
    raise RuntimeError(f"{what}: {detail[:1000]}")


def gh_get_meta(repo: str, path: str) -> dict | None:
    proc = run(["gh", "api", f"repos/{repo}/contents/{path}"], timeout=30)
    if proc.returncode == 0:
        obj = json.loads(proc.stdout)
        return obj if isinstance(obj, dict) else None
    text = proc.stderr or proc.stdout or ""
    if "HTTP 404" in text or "Not Found" in text:
        return None
    _gh_error(proc, f"GitHub GET metadata {path}")


def gh_get_text(repo: str, path: str) -> str | None:
    proc = run(
        ["gh", "api", f"repos/{repo}/contents/{path}", "-H", "Accept: application/vnd.github.raw"],
        timeout=30,
    )
    if proc.returncode == 0:
        return proc.stdout
    text = proc.stderr or proc.stdout or ""
    if "HTTP 404" in text or "Not Found" in text:
        return None
    _gh_error(proc, f"GitHub GET {path}")


def gh_list(repo: str, path: str) -> list[dict]:
    proc = run(["gh", "api", f"repos/{repo}/contents/{path}?per_page=100"], timeout=30)
    if proc.returncode != 0:
        _gh_error(proc, f"GitHub LIST {path}")
    obj = json.loads(proc.stdout)
    if not isinstance(obj, list):
        raise RuntimeError(f"GitHub LIST {path}: expected list")
    return obj


def gh_put(repo: str, path: str, obj: Any, message: str):
    payload = (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode()
    content = base64.b64encode(payload).decode()

    def once():
        meta = gh_get_meta(repo, path)
        args = [
            "gh",
            "api",
            "--method",
            "PUT",
            f"repos/{repo}/contents/{path}",
            "-f",
            f"message={message}",
            "-f",
            f"content={content}",
        ]
        if meta and meta.get("sha"):
            args += ["-f", f"sha={meta['sha']}"]
        proc = run(args, timeout=45)
        if proc.returncode != 0:
            _gh_error(proc, f"GitHub PUT {path}")
        return True

    return retry(once)


def gh_delete(repo: str, path: str, sha: str | None, message: str):
    def once():
        nonlocal sha
        current_sha = sha
        if not current_sha:
            meta = gh_get_meta(repo, path)
            if meta is None:
                return True
            current_sha = str(meta.get("sha") or "")
        proc = run(
            [
                "gh",
                "api",
                "--method",
                "DELETE",
                f"repos/{repo}/contents/{path}",
                "-f",
                f"message={message}",
                "-f",
                f"sha={current_sha}",
            ],
            timeout=45,
        )
        if proc.returncode == 0:
            return True
        text = proc.stderr or proc.stdout or ""
        if "HTTP 404" in text or "Not Found" in text:
            return True
        sha = None
        _gh_error(proc, f"GitHub DELETE {path}")

    return retry(once)


def normalize_id(value: Any, kind: str) -> str:
    text = str(value or "").strip()
    if not SAFE_ID.fullmatch(text):
        raise ValueError(f"invalid {kind}: {text!r}")
    return text


def sessions_from_cfg(cfg: dict) -> list[str]:
    sessions = cfg.get("sessions") or ["default"]
    if not isinstance(sessions, list) or not sessions:
        raise ValueError("config sessions must be a non-empty list")
    out = []
    for session in sessions:
        s = normalize_id(session, "session")
        if s not in out:
            out.append(s)
    return out


def normalize_session(cfg: dict, value: Any) -> str:
    session = normalize_id(value or "default", "session")
    if session not in sessions_from_cfg(cfg):
        raise ValueError(f"unknown session {session!r}; configured: {sessions_from_cfg(cfg)}")
    return session


def normalize_job_id(session: str, value: Any) -> str:
    jid = str(value or "").strip()
    prefix = session + "--"
    while jid.startswith(prefix):
        jid = jid[len(prefix):]
    return normalize_id(jid, "job_id")


def resolve_path(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path))).resolve()


def ensure_allowed(path: str, cfg: dict, *, exists=False, directory=False) -> Path:
    target = resolve_path(path)
    roots = [resolve_path(root) for root in cfg.get("allowed_roots") or []]
    if not roots:
        raise PermissionError("no allowed_roots configured")
    if not any(target == root or root in target.parents for root in roots):
        raise PermissionError(f"outside allowed_roots: {target}")
    if exists and not target.exists():
        raise FileNotFoundError(str(target))
    if directory and target.exists() and not target.is_dir():
        raise NotADirectoryError(str(target))
    return target


def clip(text: str, limit=28000):
    if len(text) <= limit:
        return text, False
    half = limit // 2
    return text[:half] + "\n...[truncated; full output saved locally]...\n" + text[-half:], True


def artifact_path(session: str, job_id: str, name: str) -> Path:
    directory = ARTIFACT_DIR / f"{session}--{job_id}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def save_artifact(session: str, job_id: str, name: str, data: str) -> str:
    path = artifact_path(session, job_id, name)
    path.write_text(data, encoding="utf-8", errors="replace")
    return str(path)


def history_add(item: dict):
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    try:
        lines = HISTORY.read_text(encoding="utf-8").splitlines()
        if len(lines) > 500:
            HISTORY.write_text("\n".join(lines[-300:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def state_path(session: str, job_id: str) -> Path:
    digest = hashlib.sha256(f"{session}--{job_id}".encode()).hexdigest()
    return STATE_DIR / f"{digest}.json"


def read_state(session: str, job_id: str):
    return read_json(state_path(session, job_id), None)


def write_state(session: str, job_id: str, obj: dict):
    data = dict(obj)
    data.update(session=session, job_id=job_id, updated_unix=int(time.time()))
    write_json(state_path(session, job_id), data)


def payload_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class Sessions:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.names = sessions_from_cfg(cfg)
        old = read_json(CONTROL_STATE, {}) or {}
        self.lock = threading.Lock()
        self.data = {}
        now = int(time.time())
        for s in self.names:
            item = old.get(s, {}) if isinstance(old, dict) else {}
            self.data[s] = {
                "mode": item.get("mode", "running"),
                "priority": item.get("priority", ""),
                "messages": list(item.get("messages", []))[-30:],
                "updated": int(item.get("updated", now)),
            }

    def snap(self, session: str | None = None):
        with self.lock:
            obj = self.data[session] if session else self.data
            return json.loads(json.dumps(obj))

    def save(self):
        write_json(CONTROL_STATE, self.snap())

    def allowed(self, session: str):
        mode = self.snap(session)["mode"]
        return mode == "running", "" if mode == "running" else f"session-{mode}"

    def apply(self, session: str, action: str, text: str = "", source="github-control"):
        action = str(action).upper()
        now = int(time.time())
        with self.lock:
            item = self.data[session]
            if action == "PAUSE":
                item["mode"] = "paused"
            elif action == "RESUME":
                item["mode"] = "running"
            elif action == "STOP":
                item["mode"] = "stopped"
            elif action == "NOTE":
                if not text.strip():
                    raise ValueError("NOTE requires text")
            elif action == "PRIORITY":
                if not text.strip():
                    raise ValueError("PRIORITY requires text")
                item["priority"] = text.strip()
            elif action == "CLEAR_PRIORITY":
                item["priority"] = ""
            else:
                raise ValueError(f"unsupported control action {action!r}")
            message = {
                "id": f"{session}-{now}-{secrets.token_hex(3)}",
                "action": action,
                "text": text.strip(),
                "source": source,
                "unix": now,
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
            return [
                {
                    "key": key,
                    "job_id": info["job_id"],
                    "session": info["session"],
                    "started_unix": info["unix"],
                    "elapsed_s": round(now - info["mono"], 1),
                    "command": info.get("command", "")[:180],
                }
                for key, info in self.active.items()
            ]

    def cancel(self, session: str, job_id: str):
        key = f"{session}--{job_id}"
        with self.lock:
            info = self.active.get(key)
        if not info:
            return {"cancelled": False, "reason": "not-running"}
        try:
            os.killpg(info["process"].pid, signal.SIGTERM)
            return {"cancelled": True, "session": session, "job_id": job_id}
        except Exception as exc:
            return {"cancelled": False, "reason": str(exc)}

    def cancel_session(self, session: str):
        with self.lock:
            ids = [info["job_id"] for info in self.active.values() if info["session"] == session]
        return {jid: self.cancel(session, jid) for jid in ids}

    def shell(self, job: dict):
        session = normalize_session(self.cfg, job.get("session"))
        job_id = normalize_job_id(session, job.get("job_id"))
        cwd = ensure_allowed(str(job["cwd"]), self.cfg, exists=True, directory=True)
        command = str(job["command"])
        timeout = max(1, min(int(job.get("timeout", 120)), int(self.cfg.get("max_timeout", 1800))))
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (job.get("env") or {}).items()})
        started = time.monotonic()
        proc = subprocess.Popen(
            [self.cfg.get("shell", "/bin/bash"), "-lc", command],
            cwd=str(cwd),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        key = f"{session}--{job_id}"
        with self.lock:
            self.active[key] = {
                "process": proc,
                "session": session,
                "job_id": job_id,
                "mono": started,
                "unix": int(time.time()),
                "command": command,
            }
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                pass
            try:
                out, err = proc.communicate(timeout=4)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
                out, err = proc.communicate()
        finally:
            with self.lock:
                self.active.pop(key, None)
        out = out or ""
        err = err or ""
        stdout_artifact = save_artifact(session, job_id, "stdout.log", out)
        stderr_artifact = save_artifact(session, job_id, "stderr.log", err)
        stdout, stdout_truncated = clip(out)
        stderr, stderr_truncated = clip(err)
        return {
            "exit_code": 124 if timed_out else proc.returncode,
            "timed_out": timed_out,
            "duration_s": round(time.monotonic() - started, 3),
            "cwd": str(cwd),
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "stdout_artifact": stdout_artifact,
            "stderr_artifact": stderr_artifact,
        }

    def execute(self, job: dict):
        session = normalize_session(self.cfg, job.get("session"))
        job["session"] = session
        op = str(job.get("op") or "")
        if op == "ping":
            return {"pong": True, "host": socket.gethostname(), "time": int(time.time()), "session": session}
        if op == "shell":
            return self.shell(job)
        if op == "cancel":
            target_session = normalize_session(self.cfg, job.get("target_session") or session)
            target_job = normalize_job_id(target_session, job.get("target_job_id"))
            return self.cancel(target_session, target_job)
        if op == "control_status":
            return self.sessions.snap(session)
        if op == "read_file":
            path = ensure_allowed(str(job["path"]), self.cfg, exists=True)
            text = path.read_text(encoding="utf-8", errors="replace")
            start = max(1, int(job.get("start_line", 1)))
            end = int(job.get("end_line", start + 399))
            lines = text.splitlines()
            end = min(len(lines), end)
            return {"path": str(path), "content": "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))}
        if op == "write_file":
            path = ensure_allowed(str(job["path"]), self.cfg)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(job.get("content", "")), encoding="utf-8")
            return {"path": str(path), "bytes": path.stat().st_size}
        if op == "git_status":
            cwd = ensure_allowed(str(job["cwd"]), self.cfg, exists=True, directory=True)
            proc = run(["git", "-C", str(cwd), "status", "--short", "--branch"], timeout=60)
            return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
        if op == "git_diff":
            cwd = ensure_allowed(str(job["cwd"]), self.cfg, exists=True, directory=True)
            args = ["git", "-C", str(cwd), "diff", "--no-ext-diff", "--unified=3"]
            if job.get("cached"):
                args.insert(4, "--cached")
            proc = run(args, timeout=60)
            stdout, truncated = clip(proc.stdout or "")
            return {"exit_code": proc.returncode, "stdout": stdout, "stdout_truncated": truncated, "stderr": proc.stderr}
        if op == "list_files":
            root = ensure_allowed(str(job["path"]), self.cfg, exists=True, directory=True)
            limit = min(int(job.get("max_entries", 500)), 3000)
            depth = min(int(job.get("max_depth", 3)), 8)
            output = []
            base_parts = len(root.parts)
            for current, dirs, files in os.walk(root):
                current_path = Path(current)
                if len(current_path.parts) - base_parts >= depth:
                    dirs[:] = []
                dirs[:] = [d for d in dirs if d not in {".git", ".cache", "node_modules"}]
                for name in sorted(dirs):
                    output.append(str((current_path / name).relative_to(root)) + "/")
                for name in sorted(files):
                    output.append(str((current_path / name).relative_to(root)))
                if len(output) >= limit:
                    return {"entries": output[:limit], "truncated": True}
            return {"entries": output, "truncated": False}
        raise ValueError(f"unsupported op {op!r}")


class Transport:
    def __init__(self, cfg: dict, runner: Runner, sessions: Sessions):
        self.cfg = cfg
        self.runner = runner
        self.sessions = sessions
        self.repo = str(cfg["repo"])
        self.prefix = str(cfg.get("queue_prefix", "relay"))
        self.lock = threading.Lock()
        self.inflight: set[str] = set()
        self.claimed: set[str] = set()
        self.last_queue_ok = 0
        self.last_control_ok = 0
        self.consecutive_api_errors = 0
        self.last_api_error = ""
        self.recovered_jobs = 0
        self.malformed_quarantined = 0
        self.queue_manifest_updates = 0
        self._manifest_fingerprint: str | None = None

    def result_path(self, session: str, job_id: str):
        return f"{self.prefix}/results/{session}--{job_id}.json"

    def api_ok(self, kind: str):
        now = int(time.time())
        if kind == "queue":
            self.last_queue_ok = now
        elif kind == "control":
            self.last_control_ok = now
        self.consecutive_api_errors = 0
        self.last_api_error = ""

    def api_error(self, exc: Exception):
        self.consecutive_api_errors += 1
        self.last_api_error = f"{type(exc).__name__}: {exc}"[:500]

    def _live_worker_sessions(self):
        return {t.name[6:] for t in threading.enumerate() if t.is_alive() and t.name.startswith("relay-")}

    def reconcile_claims(self):
        live = self._live_worker_sessions()
        active = {x["session"] for x in self.runner.snapshot()}
        with self.lock:
            stale = [s for s in self.claimed if s not in live and s not in active]
            for session in stale:
                self.claimed.discard(session)
                prefix = session + "--"
                for name in list(self.inflight):
                    if name.startswith(prefix):
                        self.inflight.discard(name)
            if not live and not active:
                self.claimed.clear()
                self.inflight.clear()
        return stale

    def status_snapshot(self):
        now = int(time.time())
        stale = self.reconcile_claims()
        with self.lock:
            inflight = sorted(self.inflight)
            claimed = sorted(self.claimed)
        return {
            "queue_last_ok_unix": self.last_queue_ok,
            "queue_last_ok_age_s": None if not self.last_queue_ok else now - self.last_queue_ok,
            "control_last_ok_unix": self.last_control_ok,
            "consecutive_api_errors": self.consecutive_api_errors,
            "last_api_error": self.last_api_error,
            "inflight_jobs": inflight,
            "claimed_sessions": claimed,
            "stale_claims": stale,
            "recovered_jobs": self.recovered_jobs,
            "malformed_quarantined": self.malformed_quarantined,
            "queue_manifest_updates": self.queue_manifest_updates,
            "queue_manifest_path": f"{self.prefix}/status/queue.json",
        }

    def publish_hello(self):
        body = {
            "protocol": PROTOCOL,
            "relay_version": VERSION,
            "host": socket.gethostname(),
            "sessions": sessions_from_cfg(self.cfg),
            "allowed_roots": self.cfg.get("allowed_roots", []),
            "job_filename": "<session>--<job_id>.json",
            "result_filename": "<session>--<job_id>.json",
            "queue_manifest": f"{self.prefix}/status/queue.json",
            "github_auth_model": "write access to this queue repository is trusted",
            "durable_job_state": True,
            "malformed_job_quarantine": True,
            "local_http": f"{self.cfg.get('http_host', '127.0.0.1')}:{self.cfg.get('http_port', 8765)}",
        }
        gh_put(self.repo, f"{self.prefix}/status/hello.json", body, "chatgpt-relay hello")

    def publish_status(self):
        body = {
            "protocol": PROTOCOL,
            "relay_version": VERSION,
            "host": socket.gethostname(),
            "unix": int(time.time()),
            "active_jobs": self.runner.snapshot(),
            "sessions": self.sessions.snap(),
            "transport": self.status_snapshot(),
        }
        gh_put(self.repo, f"{self.prefix}/status/heartbeat.json", body, "chatgpt-relay heartbeat")
        gh_put(
            self.repo,
            f"{self.prefix}/status/sessions.json",
            {"protocol": PROTOCOL, "relay_version": VERSION, "unix": int(time.time()), "sessions": self.sessions.snap()},
            "chatgpt-relay sessions",
        )

    def _publish_queue_manifest(self, pending: list[dict], malformed: list[dict]):
        with self.lock:
            inflight = sorted(self.inflight)
            claimed = sorted(self.claimed)
        semantic = {
            "pending": pending,
            "malformed": malformed,
            "inflight_jobs": inflight,
            "claimed_sessions": claimed,
            "active_jobs": [{"job_id": x.get("job_id"), "session": x.get("session")} for x in self.runner.snapshot()],
            "sessions": {s: {"mode": self.sessions.snap(s).get("mode")} for s in sessions_from_cfg(self.cfg)},
        }
        fingerprint = hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if fingerprint == self._manifest_fingerprint:
            return
        body = {"protocol": PROTOCOL, "relay_version": VERSION, "host": socket.gethostname(), "unix": int(time.time()), **semantic}
        gh_put(self.repo, f"{self.prefix}/status/queue.json", body, "chatgpt-relay queue index")
        self._manifest_fingerprint = fingerprint
        self.queue_manifest_updates += 1

    def _infer_identity(self, filename: str):
        stem = filename[:-5] if filename.endswith(".json") else filename
        if "--" in stem:
            session_raw, jid_raw = stem.split("--", 1)
        else:
            session_raw, jid_raw = sessions_from_cfg(self.cfg)[0], stem
        try:
            session = normalize_session(self.cfg, session_raw)
        except Exception:
            session = sessions_from_cfg(self.cfg)[0]
        try:
            jid = normalize_job_id(session, jid_raw)
        except Exception:
            jid = hashlib.sha256(filename.encode()).hexdigest()[:16]
        return session, jid

    def _result_base(self, session: str, job_id: str):
        return {
            "protocol": PROTOCOL,
            "relay_version": VERSION,
            "job_id": job_id,
            "session": session,
            "host": socket.gethostname(),
            "control": self.sessions.snap(session),
        }

    def _publish_confirmed(self, session: str, job_id: str, result: dict, item: dict, filename: str):
        result_path = self.result_path(session, job_id)
        gh_put(self.repo, result_path, result, f"chatgpt-relay result {session} {job_id}")
        remote = gh_get_text(self.repo, result_path)
        if remote is None:
            raise RuntimeError(f"result verification failed: {result_path}")
        parsed = json.loads(remote)
        if parsed.get("job_id") != job_id or parsed.get("session") != session:
            raise RuntimeError(f"result verification mismatch: {result_path}")
        gh_delete(
            self.repo,
            f"{self.prefix}/jobs/{filename}",
            str(item.get("sha") or "") or None,
            f"chatgpt-relay consumed {session} {job_id}",
        )
        write_state(session, job_id, {"state": "published", "result": result, "published_unix": int(time.time())})

    def _quarantine_malformed(self, item: dict, filename: str, exc: Exception):
        session, job_id = self._infer_identity(filename)
        result = self._result_base(session, job_id)
        result.update(
            status="error",
            error=f"malformed_job_json: {type(exc).__name__}: {exc}"[:700],
            result={},
            source_filename=filename,
        )
        result_path = self.result_path(session, job_id)
        if gh_get_text(self.repo, result_path) is None:
            gh_put(self.repo, result_path, result, f"chatgpt-relay malformed {session} {job_id}")
        gh_delete(
            self.repo,
            f"{self.prefix}/jobs/{filename}",
            str(item.get("sha") or "") or None,
            f"chatgpt-relay quarantined malformed {session} {job_id}",
        )
        self.malformed_quarantined += 1
        history_add({"unix": int(time.time()), "session": session, "job_id": job_id, "status": "malformed-quarantined", "duration_s": 0})

    def worker(self, item: dict, filename: str, raw: str):
        session, job_id = self._infer_identity(filename)
        started = time.time()
        result = None
        try:
            job = json.loads(raw)
            if job.get("protocol") != PROTOCOL:
                raise ValueError(f"wrong protocol; expected {PROTOCOL}")
            session = normalize_session(self.cfg, job.get("session") or session)
            job_id = normalize_job_id(session, job.get("job_id") or job_id)
            job["session"] = session
            job["job_id"] = job_id
            expected = f"{session}--{job_id}.json"
            if filename != expected:
                raise ValueError(f"filename/job identity mismatch: expected {expected}")

            digest = payload_hash(raw)
            state = read_state(session, job_id)
            if state and state.get("payload_hash") not in (None, digest):
                result = self._result_base(session, job_id)
                result.update(status="error", error="job_id reused with different payload", result={})
                write_state(session, job_id, {"state": "done", "payload_hash": digest, "result": result})
                self._publish_confirmed(session, job_id, result, item, filename)
                return

            if state and state.get("state") in {"done", "published"} and state.get("result"):
                result = state["result"]
                self.recovered_jobs += 1
                self._publish_confirmed(session, job_id, result, item, filename)
                return

            if state and state.get("state") == "running" and state.get("instance") != INSTANCE:
                result = self._result_base(session, job_id)
                result.update(status="error", error="interrupted_previous_relay_instance; command was not re-executed", result={})
                write_state(session, job_id, {"state": "done", "payload_hash": digest, "result": result})
                self._publish_confirmed(session, job_id, result, item, filename)
                return

            remote = gh_get_text(self.repo, self.result_path(session, job_id))
            if remote is not None:
                gh_delete(
                    self.repo,
                    f"{self.prefix}/jobs/{filename}",
                    str(item.get("sha") or "") or None,
                    f"chatgpt-relay duplicate consumed {session} {job_id}",
                )
                history_add({"unix": int(time.time()), "session": session, "job_id": job_id, "status": "duplicate", "duration_s": round(time.time() - started, 2)})
                return

            allowed, reason = self.sessions.allowed(session)
            result = self._result_base(session, job_id)
            if not allowed:
                result.update(status="blocked", error=reason, result={})
            else:
                write_state(
                    session,
                    job_id,
                    {"state": "running", "instance": INSTANCE, "payload_hash": digest, "started_unix": int(time.time())},
                )
                try:
                    result.update(status="ok", result=self.runner.execute(job))
                except Exception as exc:
                    result.update(status="error", error=f"{type(exc).__name__}: {exc}"[:1000], result={})

            write_state(
                session,
                job_id,
                {"state": "done", "instance": INSTANCE, "payload_hash": digest, "result": result},
            )
            self._publish_confirmed(session, job_id, result, item, filename)
        except Exception as exc:
            try:
                if result is None:
                    result = self._result_base(session, job_id)
                    result.update(status="error", error=f"{type(exc).__name__}: {exc}"[:1000], result={})
                write_state(
                    session,
                    job_id,
                    {"state": "done", "instance": INSTANCE, "payload_hash": payload_hash(raw), "result": result},
                )
                self._publish_confirmed(session, job_id, result, item, filename)
            except Exception as publish_exc:
                self.api_error(publish_exc)
        finally:
            history_add(
                {
                    "unix": int(time.time()),
                    "session": session,
                    "job_id": job_id,
                    "status": (result or {}).get("status", "transport-error"),
                    "duration_s": round(time.time() - started, 2),
                }
            )
            with self.lock:
                self.inflight.discard(filename)
                self.claimed.discard(session)

    def poll_jobs(self):
        self.reconcile_claims()
        items = gh_list(self.repo, f"{self.prefix}/jobs")
        self.api_ok("queue")
        parsed = []
        malformed = []
        launched = 0

        for item in items:
            filename = str(item.get("name") or "")
            if not filename.endswith(".json"):
                continue
            try:
                raw = gh_get_text(self.repo, f"{self.prefix}/jobs/{filename}")
                if raw is None:
                    continue
                job = json.loads(raw)
                session = normalize_session(self.cfg, job.get("session") or self._infer_identity(filename)[0])
                job_id = normalize_job_id(session, job.get("job_id") or self._infer_identity(filename)[1])
            except json.JSONDecodeError as exc:
                record = {"filename": filename, "error": f"{type(exc).__name__}: {exc}"[:500]}
                try:
                    session, job_id = self._infer_identity(filename)
                    record.update(session=session, job_id=job_id)
                    self._quarantine_malformed(item, filename, exc)
                    record["state"] = "quarantined"
                except Exception as quarantine_exc:
                    record["state"] = "quarantine-failed"
                    record["quarantine_error"] = f"{type(quarantine_exc).__name__}: {quarantine_exc}"[:500]
                    malformed.append(record)
                    self.api_error(quarantine_exc)
                continue
            except Exception:
                raw = gh_get_text(self.repo, f"{self.prefix}/jobs/{filename}")
                if raw is None:
                    continue
                session, job_id = self._infer_identity(filename)

            with self.lock:
                already = filename in self.inflight
                claimed = session in self.claimed
            parsed.append(
                {
                    "filename": filename,
                    "session": session,
                    "job_id": job_id,
                    "state": "inflight" if already else ("waiting-session" if claimed else "queued"),
                    "sha": str(item.get("sha") or ""),
                }
            )
            if already or claimed:
                continue
            with self.lock:
                if filename in self.inflight or session in self.claimed:
                    continue
                self.inflight.add(filename)
                self.claimed.add(session)
            threading.Thread(target=self.worker, args=(item, filename, raw), daemon=True, name=f"relay-{session}").start()
            launched += 1

        with self.lock:
            inflight_now = set(self.inflight)
            claimed_now = set(self.claimed)
        for record in parsed:
            if record["filename"] in inflight_now:
                record["state"] = "inflight"
            elif record["session"] in claimed_now:
                record["state"] = "waiting-session"
            else:
                record["state"] = "queued"
        self._publish_queue_manifest(parsed, malformed)
        return launched

    def poll_controls(self):
        items = gh_list(self.repo, f"{self.prefix}/control")
        self.api_ok("control")
        for item in items:
            filename = str(item.get("name") or "")
            if not filename.endswith(".json"):
                continue
            path = f"{self.prefix}/control/{filename}"
            try:
                raw = gh_get_text(self.repo, path)
                if raw is None:
                    continue
                control = json.loads(raw)
                if control.get("protocol") != PROTOCOL:
                    raise ValueError(f"wrong protocol; expected {PROTOCOL}")
                session = normalize_session(self.cfg, control.get("session") or self._infer_identity(filename)[0])
                action = str(control.get("action") or "").upper()
                message = self.sessions.apply(session, action, str(control.get("text") or ""))
                if action == "STOP":
                    self.runner.cancel_session(session)
                result = {
                    "protocol": PROTOCOL,
                    "relay_version": VERSION,
                    "status": "ok",
                    "session": session,
                    "control": message,
                    "state": self.sessions.snap(session),
                }
            except Exception as exc:
                session, _ = self._infer_identity(filename)
                result = {
                    "protocol": PROTOCOL,
                    "relay_version": VERSION,
                    "status": "error",
                    "session": session,
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                }
            gh_put(self.repo, f"{self.prefix}/control-results/{filename}", result, f"chatgpt-relay control {filename}")
            gh_delete(
                self.repo,
                path,
                str(item.get("sha") or "") or None,
                f"chatgpt-relay consumed control {filename}",
            )

    def loop(self):
        self.publish_hello()
        last_heartbeat = 0.0
        last_control = 0.0
        while True:
            now = time.time()
            if now - last_control >= 6:
                try:
                    self.poll_controls()
                except Exception as exc:
                    self.api_error(exc)
                last_control = now
            try:
                self.poll_jobs()
            except Exception as exc:
                self.api_error(exc)
            now = time.time()
            if now - last_heartbeat >= float(self.cfg.get("heartbeat_seconds", 300)):
                try:
                    self.publish_status()
                except Exception as exc:
                    self.api_error(exc)
                last_heartbeat = now
            time.sleep(float(self.cfg.get("poll_seconds", 3)))


class Handler(BaseHTTPRequestHandler):
    server_version = "ChatGPTRelay/1"

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
        if parsed.path.startswith("/artifact/"):
            rel = urllib.parse.unquote(parsed.path[len("/artifact/"):])
            path = (ARTIFACT_DIR / rel).resolve()
            root = ARTIFACT_DIR.resolve()
            if path != root and root not in path.parents:
                return self.send_json(403, {"error": "bad path"})
            if not path.is_file():
                return self.send_json(404, {"error": "not found"})
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
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
            result = self.server.runner.execute(job)
            return self.send_json(200, {"job_id": job["job_id"], "status": "ok", "result": result})
        except Exception as exc:
            return self.send_json(400, {"status": "error", "error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, *_args):
        pass


def init_config(args):
    roots = [str(resolve_path(root)) for root in args.root]
    for root in roots:
        if not Path(root).exists():
            raise SystemExit(f"workspace root does not exist: {root}")
    sessions = args.session or ["default"]
    sessions = [normalize_id(s, "session") for s in sessions]
    old = read_json(CFG_PATH, {}) or {}
    config = {
        "protocol": PROTOCOL,
        "repo": args.repo,
        "queue_prefix": args.queue_prefix,
        "local_api_token": old.get("local_api_token") or secrets.token_urlsafe(32),
        "allowed_roots": roots,
        "sessions": list(dict.fromkeys(sessions)),
        "poll_seconds": args.poll_seconds,
        "heartbeat_seconds": args.heartbeat_seconds,
        "shell": args.shell,
        "max_timeout": args.max_timeout,
        "http_host": "127.0.0.1",
        "http_port": args.http_port,
    }
    write_json(CFG_PATH, config)
    print(CFG_PATH)


def rotate_token():
    cfg = read_json(CFG_PATH)
    if not cfg:
        raise SystemExit(f"missing config: {CFG_PATH}")
    cfg["local_api_token"] = secrets.token_urlsafe(32)
    write_json(CFG_PATH, cfg)
    print(cfg["local_api_token"])


def run_daemon():
    cfg = read_json(CFG_PATH)
    if not cfg:
        raise SystemExit(f"missing config: {CFG_PATH}")
    sessions = Sessions(cfg)
    runner = Runner(cfg, sessions)
    transport = Transport(cfg, runner, sessions)
    server = ThreadingHTTPServer(
        (cfg.get("http_host", "127.0.0.1"), int(cfg.get("http_port", 8765))),
        Handler,
    )
    server.cfg = cfg
    server.runner = runner
    server.sessions = sessions
    server.transport = transport
    threading.Thread(target=server.serve_forever, daemon=True).start()
    transport.loop()


def main():
    parser = argparse.ArgumentParser(description="GitHub-backed execution relay for ChatGPT")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write relay configuration")
    init.add_argument("--repo", required=True, help="OWNER/REPO for the dedicated queue repository")
    init.add_argument("--root", action="append", required=True, help="allowed workspace root; repeatable")
    init.add_argument("--session", action="append", help="session name; repeatable (default: default)")
    init.add_argument("--queue-prefix", default="relay")
    init.add_argument("--http-port", type=int, default=8765)
    init.add_argument("--max-timeout", type=int, default=1800)
    init.add_argument("--poll-seconds", type=int, default=3)
    init.add_argument("--heartbeat-seconds", type=int, default=300)
    init.add_argument("--shell", default="/bin/bash")

    sub.add_parser("run", help="run the relay daemon")
    sub.add_parser("show-local-token", help="print the localhost HTTP API token")
    sub.add_parser("rotate-local-token", help="rotate the localhost HTTP API token")

    args = parser.parse_args()
    if args.command == "init":
        return init_config(args)
    if args.command == "run":
        return run_daemon()
    if args.command == "show-local-token":
        cfg = read_json(CFG_PATH)
        if not cfg:
            raise SystemExit(f"missing config: {CFG_PATH}")
        print(cfg.get("local_api_token", ""))
        return
    if args.command == "rotate-local-token":
        return rotate_token()


if __name__ == "__main__":
    main()
