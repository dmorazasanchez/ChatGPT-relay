from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import common

STATE_DB = common.STATE_DB
LEGACY_STATE_DIR = common.LEGACY_STATE_DIR
HISTORY = common.HISTORY


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


def db_connect() -> sqlite3.Connection:
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
            session TEXT NOT NULL, job_id TEXT NOT NULL, state TEXT NOT NULL,
            payload_hash TEXT, instance TEXT, started_unix INTEGER, updated_unix INTEGER NOT NULL,
            published_unix INTEGER, result_json TEXT, PRIMARY KEY(session, job_id)
        )"""
    )
    return conn


def state_get(session: str, job_id: str) -> dict | None:
    conn = db_connect()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE session=? AND job_id=?", (session, job_id)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    result = None
    if row["result_json"]:
        try:
            result = json.loads(row["result_json"])
        except Exception:
            pass
    return {
        "session": row["session"], "job_id": row["job_id"], "state": row["state"],
        "payload_hash": row["payload_hash"], "instance": row["instance"], "started_unix": row["started_unix"],
        "updated_unix": row["updated_unix"], "published_unix": row["published_unix"], "result": result,
    }


def state_put(session: str, job_id: str, obj: dict):
    now = int(time.time())
    result_json = json.dumps(obj.get("result"), ensure_ascii=False) if obj.get("result") is not None else None
    conn = db_connect()
    try:
        old = conn.execute("SELECT * FROM jobs WHERE session=? AND job_id=?", (session, job_id)).fetchone()
        values = {
            "state": obj.get("state") or (old["state"] if old else "done"),
            "payload_hash": obj.get("payload_hash", old["payload_hash"] if old else None),
            "instance": obj.get("instance", old["instance"] if old else None),
            "started_unix": obj.get("started_unix", old["started_unix"] if old else None),
            "published_unix": obj.get("published_unix", old["published_unix"] if old else None),
            "result_json": result_json if "result" in obj else (old["result_json"] if old else None),
        }
        conn.execute(
            """INSERT INTO jobs(session,job_id,state,payload_hash,instance,started_unix,updated_unix,published_unix,result_json)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(session,job_id) DO UPDATE SET
                 state=excluded.state,payload_hash=excluded.payload_hash,instance=excluded.instance,
                 started_unix=excluded.started_unix,updated_unix=excluded.updated_unix,
                 published_unix=excluded.published_unix,result_json=excluded.result_json""",
            (session, job_id, values["state"], values["payload_hash"], values["instance"], values["started_unix"],
             now, values["published_unix"], values["result_json"]),
        )
        conn.commit()
    finally:
        conn.close()


def migrate_legacy_state() -> int:
    if not LEGACY_STATE_DIR.is_dir():
        return 0
    migrated = 0
    for path in LEGACY_STATE_DIR.glob("*.json"):
        data = common.read_json(path, None)
        if not isinstance(data, dict) or not data.get("session") or not data.get("job_id"):
            continue
        try:
            session, job_id = str(data["session"]), str(data["job_id"])
            if state_get(session, job_id) is None:
                state_put(session, job_id, data)
                migrated += 1
        except Exception:
            continue
    return migrated


def payload_hash(raw: str) -> str:
    import hashlib
    return hashlib.sha256(raw.encode()).hexdigest()
