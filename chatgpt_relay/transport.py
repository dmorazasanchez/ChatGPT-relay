from __future__ import annotations

import hashlib
import json
import socket
import threading
import time

from .common import (
    INSTANCE,
    PROTOCOL,
    VERSION,
    JobExpiredError,
    QueueMutationConflict,
    capabilities,
    control_schema,
    job_schema,
    normalize_job_id,
    normalize_session,
    sessions_from_cfg,
    validate_control,
    decode_transport_payload,
    validate_freshness,
    validate_job,
)
from .github import gh_delete_immutable, gh_get_blob_text, gh_get_text, gh_list, gh_put
from .runner import Runner, Sessions
from .state import history_add, payload_hash, state_get, state_put

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
        self.expired_jobs = 0
        self.queue_mutation_conflicts = 0
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
            "expired_jobs": self.expired_jobs,
            "queue_mutation_conflicts": self.queue_mutation_conflicts,
            "queue_manifest_updates": self.queue_manifest_updates,
            "queue_manifest_path": f"{self.prefix}/status/queue.json",
            "state_backend": "sqlite-wal",
        }

    def publish_protocol_metadata(self):
        caps = capabilities(self.cfg)
        hello = {
            "protocol": PROTOCOL,
            "relay_version": VERSION,
            "host": socket.gethostname(),
            "sessions": sessions_from_cfg(self.cfg),
            "allowed_roots": self.cfg.get("allowed_roots", []),
            "job_filename": "<session>--<job_id>.json",
            "result_filename": "<session>--<job_id>.json",
            "queue_manifest": f"{self.prefix}/status/queue.json",
            "capabilities": f"{self.prefix}/status/capabilities.json",
            "job_schema": f"{self.prefix}/status/job.schema.json",
            "control_schema": f"{self.prefix}/status/control.schema.json",
            "github_auth_model": "write access to this queue repository is trusted",
            "durable_job_state": True,
            "malformed_job_quarantine": True,
            "immutable_queue_blobs": True,
            "streaming_output": True,
            "local_http": f"{self.cfg.get('http_host', '127.0.0.1')}:{self.cfg.get('http_port', 8765)}",
        }
        gh_put(self.repo, f"{self.prefix}/status/hello.json", hello, "chatgpt-relay hello")
        gh_put(self.repo, f"{self.prefix}/status/capabilities.json", caps, "chatgpt-relay capabilities")
        gh_put(self.repo, f"{self.prefix}/status/job.schema.json", job_schema(self.cfg), "chatgpt-relay job schema")
        gh_put(self.repo, f"{self.prefix}/status/control.schema.json", control_schema(self.cfg), "chatgpt-relay control schema")

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

    def _result_base(self, session: str, job_id: str, source_blob_sha: str | None = None):
        body = {
            "protocol": PROTOCOL,
            "relay_version": VERSION,
            "job_id": job_id,
            "session": session,
            "host": socket.gethostname(),
            "control": self.sessions.snap(session),
        }
        if source_blob_sha:
            body["source_blob_sha"] = source_blob_sha
        return body

    def _publish_confirmed(self, session: str, job_id: str, result: dict, filename: str, selected_sha: str):
        result_path = self.result_path(session, job_id)
        result["source_blob_sha"] = selected_sha
        gh_put(self.repo, result_path, result, f"chatgpt-relay result {session} {job_id}")
        remote = gh_get_text(self.repo, result_path)
        if remote is None:
            raise RuntimeError(f"result verification failed: {result_path}")
        parsed = json.loads(remote)
        if (
            parsed.get("job_id") != job_id
            or parsed.get("session") != session
            or parsed.get("source_blob_sha") != selected_sha
        ):
            raise RuntimeError(f"result verification mismatch: {result_path}")
        gh_delete_immutable(
            self.repo,
            f"{self.prefix}/jobs/{filename}",
            selected_sha,
            f"chatgpt-relay consumed {session} {job_id}",
        )
        state_put(
            session,
            job_id,
            {"state": "published", "result": result, "published_unix": int(time.time())},
        )

    def _quarantine_invalid(self, filename: str, selected_sha: str, exc: Exception, reason="invalid_job_payload"):
        session, job_id = self._infer_identity(filename)
        result = self._result_base(session, job_id, selected_sha)
        result.update(
            status="error",
            error=f"{reason}: {type(exc).__name__}: {exc}"[:700],
            result={},
            source_filename=filename,
        )
        result_path = self.result_path(session, job_id)
        remote = gh_get_text(self.repo, result_path)
        same_blob = False
        if remote is not None:
            try:
                same_blob = json.loads(remote).get("source_blob_sha") == selected_sha
            except Exception:
                same_blob = False
        if not same_blob:
            gh_put(self.repo, result_path, result, f"chatgpt-relay invalid {session} {job_id}")
        gh_delete_immutable(
            self.repo,
            f"{self.prefix}/jobs/{filename}",
            selected_sha,
            f"chatgpt-relay quarantined invalid {session} {job_id}",
        )
        self.malformed_quarantined += 1
        history_add({"unix": int(time.time()), "session": session, "job_id": job_id, "status": "invalid-quarantined", "duration_s": 0})

    def _quarantine_malformed(self, filename: str, selected_sha: str, exc: Exception):
        return self._quarantine_invalid(filename, selected_sha, exc, reason="malformed_job_json")

    def worker(self, filename: str, selected_sha: str, raw: str):
        session, job_id = self._infer_identity(filename)
        started = time.time()
        result = None
        history_status = "transport-error"
        try:
            job, payload_encoding = decode_transport_payload(raw)
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
            state = state_get(session, job_id)
            if state and state.get("payload_hash") not in (None, digest):
                result = self._result_base(session, job_id, selected_sha)
                result.update(status="error", error="job_id reused with different payload", result={})
                state_put(session, job_id, {"state": "done", "payload_hash": digest, "result": result})
                self._publish_confirmed(session, job_id, result, filename, selected_sha)
                history_status = "error"
                return
            if state and state.get("state") in {"done", "published"} and state.get("result"):
                result = state["result"]
                self.recovered_jobs += 1
                self._publish_confirmed(session, job_id, result, filename, selected_sha)
                history_status = str(result.get("status") or "ok")
                return
            if state and state.get("state") == "running" and state.get("instance") != INSTANCE:
                result = self._result_base(session, job_id, selected_sha)
                result.update(status="error", error="interrupted_previous_relay_instance; command was not re-executed", result={})
                state_put(session, job_id, {"state": "done", "payload_hash": digest, "result": result})
                self._publish_confirmed(session, job_id, result, filename, selected_sha)
                history_status = "error"
                return

            remote_raw = gh_get_text(self.repo, self.result_path(session, job_id))
            if remote_raw is not None:
                try:
                    remote = json.loads(remote_raw)
                except Exception:
                    remote = {}
                if remote.get("source_blob_sha") == selected_sha:
                    gh_delete_immutable(
                        self.repo,
                        f"{self.prefix}/jobs/{filename}",
                        selected_sha,
                        f"chatgpt-relay duplicate consumed {session} {job_id}",
                    )
                    history_status = "duplicate"
                    return

            try:
                validate_job(self.cfg, job, enforce_freshness=True)
            except JobExpiredError as exc:
                self.expired_jobs += 1
                result = self._result_base(session, job_id, selected_sha)
                result.update(status="expired", error=str(exc), result={})
                state_put(session, job_id, {"state": "done", "payload_hash": digest, "result": result})
                self._publish_confirmed(session, job_id, result, filename, selected_sha)
                history_status = "expired"
                return

            allowed, reason = self.sessions.allowed(session)
            result = self._result_base(session, job_id, selected_sha)
            result["source_payload_encoding"] = payload_encoding
            if not allowed:
                result.update(status="blocked", error=reason, result={})
            else:
                state_put(
                    session,
                    job_id,
                    {"state": "running", "instance": INSTANCE, "payload_hash": digest, "started_unix": int(time.time())},
                )
                try:
                    result.update(status="ok", result=self.runner.execute(job))
                except Exception as exc:
                    result.update(status="error", error=f"{type(exc).__name__}: {exc}"[:1000], result={})

            state_put(
                session,
                job_id,
                {"state": "done", "instance": INSTANCE, "payload_hash": digest, "result": result},
            )
            self._publish_confirmed(session, job_id, result, filename, selected_sha)
            history_status = str(result.get("status") or "ok")
        except QueueMutationConflict as exc:
            self.queue_mutation_conflicts += 1
            self.api_error(exc)
            history_status = "job-changed-conflict"
        except Exception as exc:
            try:
                if result is None:
                    result = self._result_base(session, job_id, selected_sha)
                    result.update(status="error", error=f"{type(exc).__name__}: {exc}"[:1000], result={})
                state_put(
                    session,
                    job_id,
                    {"state": "done", "instance": INSTANCE, "payload_hash": payload_hash(raw), "result": result},
                )
                self._publish_confirmed(session, job_id, result, filename, selected_sha)
                history_status = str(result.get("status") or "error")
            except QueueMutationConflict as mutation_exc:
                self.queue_mutation_conflicts += 1
                self.api_error(mutation_exc)
                history_status = "job-changed-conflict"
            except Exception as publish_exc:
                self.api_error(publish_exc)
                history_status = "transport-error"
        finally:
            history_add(
                {
                    "unix": int(time.time()),
                    "session": session,
                    "job_id": job_id,
                    "status": history_status,
                    "duration_s": round(time.time() - started, 2),
                    "source_blob_sha": selected_sha,
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
        max_job_bytes = int(self.cfg.get("max_job_bytes", 1_000_000))

        for item in items:
            filename = str(item.get("name") or "")
            if not filename.endswith(".json"):
                continue
            selected_sha = str(item.get("sha") or "")
            if not selected_sha:
                malformed.append({"filename": filename, "state": "missing-blob-sha"})
                continue
            try:
                raw = gh_get_blob_text(self.repo, selected_sha, max_job_bytes)
            except Exception as exc:
                record = {
                    "filename": filename,
                    "blob_sha": selected_sha,
                    "state": "invalid-payload",
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                }
                try:
                    session, job_id = self._infer_identity(filename)
                    record.update(session=session, job_id=job_id)
                    self._quarantine_invalid(filename, selected_sha, exc)
                    record["state"] = "quarantined"
                except QueueMutationConflict as conflict:
                    self.queue_mutation_conflicts += 1
                    record["state"] = "changed-before-quarantine"
                    record["error"] = str(conflict)[:500]
                except Exception as quarantine_exc:
                    record["state"] = "quarantine-failed"
                    record["quarantine_error"] = f"{type(quarantine_exc).__name__}: {quarantine_exc}"[:500]
                    self.api_error(quarantine_exc)
                malformed.append(record)
                continue
            try:
                job, _payload_encoding = decode_transport_payload(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                record = {"filename": filename, "blob_sha": selected_sha, "error": f"{type(exc).__name__}: {exc}"[:500]}
                try:
                    session, job_id = self._infer_identity(filename)
                    record.update(session=session, job_id=job_id)
                    self._quarantine_malformed(filename, selected_sha, exc)
                    record["state"] = "quarantined"
                except QueueMutationConflict as conflict:
                    self.queue_mutation_conflicts += 1
                    record["state"] = "changed-before-quarantine"
                    record["error"] = str(conflict)[:500]
                except Exception as quarantine_exc:
                    record["state"] = "quarantine-failed"
                    record["quarantine_error"] = f"{type(quarantine_exc).__name__}: {quarantine_exc}"[:500]
                    self.api_error(quarantine_exc)
                malformed.append(record)
                continue
            try:
                session = normalize_session(self.cfg, job.get("session") or self._infer_identity(filename)[0])
                job_id = normalize_job_id(session, job.get("job_id") or self._infer_identity(filename)[1])
            except Exception:
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
                    "blob_sha": selected_sha,
                }
            )
            if already or claimed or raw is None:
                continue
            with self.lock:
                if filename in self.inflight or session in self.claimed:
                    continue
                self.inflight.add(filename)
                self.claimed.add(session)
            threading.Thread(
                target=self.worker,
                args=(filename, selected_sha, raw),
                daemon=True,
                name=f"relay-{session}",
            ).start()
            launched += 1

        with self.lock:
            inflight_now = set(self.inflight)
            claimed_now = set(self.claimed)
        for record in parsed:
            if "session" not in record:
                continue
            if record["filename"] in inflight_now:
                record["state"] = "inflight"
            elif record["session"] in claimed_now and record.get("state") not in {"invalid-payload"}:
                record["state"] = "waiting-session"
            elif record.get("state") not in {"invalid-payload"}:
                record["state"] = "queued"
        self._publish_queue_manifest(parsed, malformed)
        return launched

    def poll_controls(self):
        items = gh_list(self.repo, f"{self.prefix}/control")
        self.api_ok("control")
        max_job_bytes = int(self.cfg.get("max_job_bytes", 1_000_000))
        for item in items:
            filename = str(item.get("name") or "")
            if not filename.endswith(".json"):
                continue
            selected_sha = str(item.get("sha") or "")
            path = f"{self.prefix}/control/{filename}"
            try:
                if not selected_sha:
                    raise ValueError("control entry missing blob SHA")
                raw = gh_get_blob_text(self.repo, selected_sha, max_job_bytes)
                control, _payload_encoding = decode_transport_payload(raw)
                session = validate_control(self.cfg, control, enforce_freshness=True)
                action = str(control.get("action") or "").upper()
                message = self.sessions.apply(session, action, str(control.get("text") or ""))
                if action == "STOP":
                    self.runner.cancel_session(session)
                result = {
                    "protocol": PROTOCOL,
                    "relay_version": VERSION,
                    "status": "ok",
                    "session": session,
                    "source_blob_sha": selected_sha,
                    "control": message,
                    "state": self.sessions.snap(session),
                }
            except JobExpiredError as exc:
                session, _ = self._infer_identity(filename)
                result = {
                    "protocol": PROTOCOL,
                    "relay_version": VERSION,
                    "status": "expired",
                    "session": session,
                    "source_blob_sha": selected_sha,
                    "error": str(exc),
                }
            except Exception as exc:
                session, _ = self._infer_identity(filename)
                result = {
                    "protocol": PROTOCOL,
                    "relay_version": VERSION,
                    "status": "error",
                    "session": session,
                    "source_blob_sha": selected_sha,
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                }
            try:
                gh_put(self.repo, f"{self.prefix}/control-results/{filename}", result, f"chatgpt-relay control {filename}")
                gh_delete_immutable(self.repo, path, selected_sha, f"chatgpt-relay consumed control {filename}")
            except QueueMutationConflict as exc:
                self.queue_mutation_conflicts += 1
                self.api_error(exc)

    def loop(self):
        self.publish_protocol_metadata()
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


