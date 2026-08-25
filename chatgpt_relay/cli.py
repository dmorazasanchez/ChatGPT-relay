from __future__ import annotations

import argparse
import json
import secrets
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from .common import CFG_PATH, PROTOCOL, capabilities, normalize_id, read_json, resolve_path, write_json
from .runner import Runner, Sessions
from .server import Handler
from .state import db_connect, migrate_legacy_state
from .transport import Transport


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
        "require_job_timestamp": not args.allow_untimestamped_jobs,
        "default_job_ttl_seconds": args.default_job_ttl,
        "max_job_ttl_seconds": args.max_job_ttl,
        "job_clock_skew_seconds": args.job_clock_skew,
        "max_job_bytes": args.max_job_bytes,
        "result_output_limit": args.result_output_limit,
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
    db_connect().close()
    migrated = migrate_legacy_state()
    if migrated:
        print(f"migrated {migrated} legacy job-state records to SQLite", flush=True)
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

    # Keep the daemon and localhost health endpoint alive even if GitHub is
    # temporarily unreachable during protocol publication. Transport.loop()
    # already absorbs normal poll failures; this catches startup/fatal transport
    # failures and retries without making systemd flap the whole process.
    while True:
        try:
            transport.loop()
        except Exception as exc:
            transport.api_error(exc)
            print(f"GitHub transport degraded; retrying: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(min(15.0, max(1.0, float(cfg.get("poll_seconds", 3)))))


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
    init.add_argument("--default-job-ttl", type=int, default=3600)
    init.add_argument("--max-job-ttl", type=int, default=86400)
    init.add_argument("--job-clock-skew", type=int, default=300)
    init.add_argument("--allow-untimestamped-jobs", action="store_true")
    init.add_argument("--max-job-bytes", type=int, default=1_000_000)
    init.add_argument("--result-output-limit", type=int, default=28000)

    sub.add_parser("run", help="run the relay daemon")
    sub.add_parser("show-local-token", help="print the localhost HTTP API token")
    sub.add_parser("rotate-local-token", help="rotate the localhost HTTP API token")
    sub.add_parser("capabilities", help="print locally configured capabilities")

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
    if args.command == "capabilities":
        cfg = read_json(CFG_PATH)
        if not cfg:
            raise SystemExit(f"missing config: {CFG_PATH}")
        print(json.dumps(capabilities(cfg), indent=2))
        return


