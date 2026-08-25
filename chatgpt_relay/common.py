from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any

APP = "chatgpt-relay"
PROTOCOL = "CHATGPT_RELAY_V1"
VERSION = "1.2.0"
HOME = Path.home()
CFG_PATH = HOME / ".config" / APP / "config.json"
DATA_DIR = HOME / ".local" / "share" / APP
STATE_DB = DATA_DIR / "jobs.sqlite3"
LEGACY_STATE_DIR = DATA_DIR / "job-state"
ARTIFACT_DIR = DATA_DIR / "artifacts"
CONTROL_STATE = DATA_DIR / "control-state.json"
HISTORY = DATA_DIR / "history.jsonl"
INSTANCE = f"{os.getpid()}-{secrets.token_hex(5)}"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
GIT_BLOB_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
SUPPORTED_OPS = (
    "ping", "shell", "cancel", "control_status", "read_file", "write_file",
    "git_status", "git_diff", "list_files", "artifact_info", "artifact_read", "artifact_tail",
)
ARTIFACT_STREAMS = ("stdout", "stderr")


class QueueMutationConflict(RuntimeError):
    """A queue/control path no longer points at the selected immutable Git blob."""


class JobExpiredError(ValueError):
    pass


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
        args, input=input_text, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
    )


def retry(fn, *, attempts=3, base_delay=0.5):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except QueueMutationConflict:
            raise
        except Exception as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(base_delay * (2**i))
    raise RuntimeError(str(last)) from last


def normalize_id(value: Any, kind: str) -> str:
    text = str(value or "").strip()
    if not SAFE_ID.fullmatch(text):
        raise ValueError(f"invalid {kind}: {text!r}")
    return text


def sessions_from_cfg(cfg: dict) -> list[str]:
    sessions = cfg.get("sessions") or ["default"]
    if not isinstance(sessions, list) or not sessions:
        raise ValueError("config sessions must be a non-empty list")
    out: list[str] = []
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


def as_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        return int(value)
    except Exception as exc:
        raise ValueError(f"{name} must be an integer") from exc


def ttl_policy(cfg: dict) -> dict:
    return {
        "required_timestamp": bool(cfg.get("require_job_timestamp", True)),
        "default_ttl_seconds": max(1, int(cfg.get("default_job_ttl_seconds", 3600))),
        "max_ttl_seconds": max(1, int(cfg.get("max_job_ttl_seconds", 86400))),
        "clock_skew_seconds": max(0, int(cfg.get("job_clock_skew_seconds", 300))),
    }


def validate_freshness(cfg: dict, payload: dict, *, now: int | None = None, kind="job") -> dict:
    policy = ttl_policy(cfg)
    now = int(time.time()) if now is None else int(now)
    created_raw = payload.get("created_unix")
    if created_raw is None:
        if policy["required_timestamp"]:
            raise ValueError(f"{kind} requires created_unix")
        return {"created_unix": None, "ttl_seconds": None, "expires_unix": None}
    created = as_int(created_raw, "created_unix")
    ttl = as_int(payload.get("ttl_seconds", policy["default_ttl_seconds"]), "ttl_seconds")
    if ttl < 1 or ttl > policy["max_ttl_seconds"]:
        raise ValueError(f"ttl_seconds must be between 1 and {policy['max_ttl_seconds']}")
    if created > now + policy["clock_skew_seconds"]:
        raise ValueError(f"{kind} created_unix is too far in the future")
    expires = created + ttl
    if now > expires:
        raise JobExpiredError(f"{kind} expired at {expires}; now={now}")
    return {"created_unix": created, "ttl_seconds": ttl, "expires_unix": expires}


def _validate_optional_blob_sha(value: Any, name: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not GIT_BLOB_SHA.fullmatch(text):
        raise ValueError(f"{name} must be a 40-character Git blob SHA")
    return text.lower()


def validate_job(cfg: dict, job: dict, *, enforce_freshness=True) -> tuple[str, str, str]:
    if not isinstance(job, dict):
        raise ValueError("job must be a JSON object")
    if job.get("protocol") != PROTOCOL:
        raise ValueError(f"wrong protocol; expected {PROTOCOL}")
    session = normalize_session(cfg, job.get("session"))
    job_id = normalize_job_id(session, job.get("job_id"))
    op = str(job.get("op") or "")
    if op not in SUPPORTED_OPS:
        raise ValueError(f"unsupported op {op!r}")
    if enforce_freshness:
        validate_freshness(cfg, job, kind="job")
    if op == "shell":
        if not isinstance(job.get("cwd"), str) or not job["cwd"]:
            raise ValueError("shell requires cwd")
        command = job.get("command")
        command_b64 = job.get("command_b64")
        if bool(command) == bool(command_b64):
            raise ValueError("shell requires exactly one of command or command_b64")
        if command is not None and (not isinstance(command, str) or not command):
            raise ValueError("shell command must be a non-empty string")
        if command_b64 is not None and (not isinstance(command_b64, str) or not command_b64):
            raise ValueError("shell command_b64 must be a non-empty string")
        if "env" in job and not isinstance(job["env"], dict):
            raise ValueError("shell env must be an object")
        if "timeout" in job and as_int(job["timeout"], "timeout") < 1:
            raise ValueError("timeout must be >= 1")
    elif op in {"read_file", "write_file", "list_files"}:
        if not isinstance(job.get("path"), str) or not job["path"]:
            raise ValueError(f"{op} requires path")
        if op == "write_file" and not isinstance(job.get("content", ""), str):
            raise ValueError("write_file content must be a string")
    elif op in {"git_status", "git_diff"}:
        if not isinstance(job.get("cwd"), str) or not job["cwd"]:
            raise ValueError(f"{op} requires cwd")
    elif op == "cancel":
        target_session = normalize_session(cfg, job.get("target_session") or session)
        normalize_job_id(target_session, job.get("target_job_id"))
        _validate_optional_blob_sha(job.get("target_source_blob_sha"), "target_source_blob_sha")
    elif op in {"artifact_info", "artifact_read", "artifact_tail"}:
        target_session = normalize_session(cfg, job.get("target_session") or session)
        normalize_job_id(target_session, job.get("target_job_id"))
        stream = str(job.get("stream") or "")
        if stream not in ARTIFACT_STREAMS:
            raise ValueError(f"stream must be one of {ARTIFACT_STREAMS}")
        if op == "artifact_read":
            if as_int(job.get("offset", 0), "offset") < 0:
                raise ValueError("offset must be >= 0")
            if as_int(job.get("max_bytes", 65536), "max_bytes") < 1:
                raise ValueError("max_bytes must be >= 1")
        if op == "artifact_tail" and as_int(job.get("lines", 200), "lines") < 1:
            raise ValueError("lines must be >= 1")
    return session, job_id, op


def validate_control(cfg: dict, control: dict, *, enforce_freshness=True) -> str:
    if not isinstance(control, dict):
        raise ValueError("control must be a JSON object")
    if control.get("protocol") != PROTOCOL:
        raise ValueError(f"wrong protocol; expected {PROTOCOL}")
    session = normalize_session(cfg, control.get("session"))
    action = str(control.get("action") or "").upper()
    if action not in {"PAUSE", "RESUME", "STOP", "NOTE", "PRIORITY", "CLEAR_PRIORITY"}:
        raise ValueError(f"unsupported control action {action!r}")
    if action in {"NOTE", "PRIORITY"} and not str(control.get("text") or "").strip():
        raise ValueError(f"{action} requires text")
    if enforce_freshness:
        validate_freshness(cfg, control, kind="control")
    return session


def clip(text: str, limit=28000):
    if len(text) <= limit:
        return text, False
    half = limit // 2
    return text[:half] + "\n...[truncated; full output saved locally]...\n" + text[-half:], True


def job_schema(cfg: dict | None = None) -> dict:
    policy = ttl_policy(cfg or {})
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ChatGPT Relay job", "type": "object",
        "required": ["protocol", "session", "job_id", "op"] + (["created_unix"] if policy["required_timestamp"] else []),
        "properties": {
            "protocol": {"const": PROTOCOL}, "session": {"type": "string", "pattern": SAFE_ID.pattern},
            "job_id": {"type": "string", "pattern": SAFE_ID.pattern}, "op": {"enum": list(SUPPORTED_OPS)},
            "created_unix": {"type": "integer"},
            "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": policy["max_ttl_seconds"]},
            "cwd": {"type": "string"}, "command": {"type": "string"}, "command_b64": {"type": "string"},
            "timeout": {"type": "integer", "minimum": 1},
            "env": {"type": "object"}, "path": {"type": "string"}, "content": {"type": "string"},
            "target_session": {"type": "string"}, "target_job_id": {"type": "string"},
            "target_source_blob_sha": {"type": "string", "pattern": "^[0-9a-fA-F]{40}$"},
            "stream": {"enum": list(ARTIFACT_STREAMS)}, "offset": {"type": "integer", "minimum": 0},
            "max_bytes": {"type": "integer", "minimum": 1}, "lines": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": True,
    }


def control_schema(cfg: dict | None = None) -> dict:
    policy = ttl_policy(cfg or {})
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ChatGPT Relay control", "type": "object",
        "required": ["protocol", "session", "action"] + (["created_unix"] if policy["required_timestamp"] else []),
        "properties": {
            "protocol": {"const": PROTOCOL}, "session": {"type": "string", "pattern": SAFE_ID.pattern},
            "action": {"enum": ["PAUSE", "RESUME", "STOP", "NOTE", "PRIORITY", "CLEAR_PRIORITY"]},
            "text": {"type": "string"}, "created_unix": {"type": "integer"},
            "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": policy["max_ttl_seconds"]},
        },
        "additionalProperties": True,
    }


def capabilities(cfg: dict) -> dict:
    return {
        "protocol": PROTOCOL, "relay_version": VERSION, "operations": list(SUPPORTED_OPS),
        "artifact_streams": list(ARTIFACT_STREAMS), "sessions": sessions_from_cfg(cfg),
        "allowed_roots": cfg.get("allowed_roots", []),
        "reliability": {
            "immutable_queue_blobs": True, "durable_state": "sqlite-wal", "at_most_once_recovery": True,
            "streaming_stdout_stderr": True, "malformed_job_quarantine": True,
            "serialized_github_mutations": True, "github_retry_backoff": True,
            "job_scoped_cancel": True, "shell_command_b64": True,
        },
        "freshness": ttl_policy(cfg),
        "limits": {
            "max_timeout": int(cfg.get("max_timeout", 1800)), "max_job_bytes": int(cfg.get("max_job_bytes", 1_000_000)),
            "artifact_read_max_bytes": 262144, "artifact_tail_max_lines": 2000,
        },
    }
