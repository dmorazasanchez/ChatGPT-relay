import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from chatgpt_relay import common
from chatgpt_relay import github as github_mod
from chatgpt_relay import runner as runner_mod
from chatgpt_relay import state as state_mod


class ReliabilityTests(unittest.TestCase):
    def cfg(self, root):
        return {
            "allowed_roots": [str(root)],
            "sessions": ["default"],
            "shell": "/bin/bash",
            "max_timeout": 10,
            "require_job_timestamp": True,
            "default_job_ttl_seconds": 3600,
            "max_job_ttl_seconds": 86400,
            "job_clock_skew_seconds": 300,
            "result_output_limit": 8192,
        }

    def fresh_job(self, **extra):
        job = {
            "protocol": common.PROTOCOL,
            "session": "default",
            "job_id": "job-1",
            "op": "ping",
            "created_unix": int(time.time()),
            "ttl_seconds": 300,
        }
        job.update(extra)
        return job

    def test_version_and_capabilities(self):
        self.assertEqual(common.VERSION, "1.1.0")
        caps = common.capabilities(self.cfg("/tmp"))
        self.assertTrue(caps["reliability"]["immutable_queue_blobs"])
        self.assertTrue(caps["reliability"]["streaming_stdout_stderr"])
        self.assertEqual(caps["reliability"]["durable_state"], "sqlite-wal")
        self.assertIn("artifact_tail", caps["operations"])

    def test_safe_ids(self):
        self.assertEqual(common.normalize_id("abc-123_test.x", "id"), "abc-123_test.x")
        with self.assertRaises(ValueError):
            common.normalize_id("../bad", "id")

    def test_ttl_accepts_fresh_and_rejects_expired(self):
        cfg = self.cfg("/tmp")
        now = 2_000_000
        job = self.fresh_job(created_unix=now - 10, ttl_seconds=30)
        common.validate_job(cfg, job, enforce_freshness=True)
        expired = dict(job, created_unix=now - 31)
        with self.assertRaises(common.JobExpiredError):
            common.validate_freshness(cfg, expired, now=now)

    def test_ttl_rejects_far_future(self):
        cfg = self.cfg("/tmp")
        with self.assertRaises(ValueError):
            common.validate_freshness(cfg, {"created_unix": 2000, "ttl_seconds": 30}, now=1000)

    def test_streaming_output_is_bounded_but_artifact_is_full(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifacts = root / "artifacts"
            cfg = self.cfg(root)
            sessions = runner_mod.Sessions(cfg)
            runr = runner_mod.Runner(cfg, sessions)
            with mock.patch.object(runner_mod, "ARTIFACT_DIR", artifacts):
                result = runr.execute({
                    "session": "default",
                    "job_id": "big-output",
                    "op": "shell",
                    "cwd": str(root),
                    "command": "python3 -c 'import sys; sys.stdout.write(\"x\"*200000); sys.stderr.write(\"y\"*150000)'",
                    "timeout": 10,
                })
                self.assertEqual(result["exit_code"], 0)
                self.assertEqual(result["stdout_bytes"], 200000)
                self.assertEqual(result["stderr_bytes"], 150000)
                self.assertTrue(result["stdout_truncated"])
                self.assertTrue(result["stderr_truncated"])
                self.assertLess(len(result["stdout"]), 10000)
                self.assertEqual(Path(result["stdout_artifact"]).stat().st_size, 200000)
                self.assertEqual(Path(result["stderr_artifact"]).stat().st_size, 150000)

    def test_artifact_read_and_tail(self):
        with tempfile.TemporaryDirectory() as td:
            artifacts = Path(td) / "artifacts"
            with mock.patch.object(runner_mod, "ARTIFACT_DIR", artifacts):
                path = runner_mod.artifact_path("default", "a1", "stdout")
                path.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
                info = runner_mod.artifact_info("default", "a1", "stdout")
                self.assertEqual(info["size_bytes"], path.stat().st_size)
                read = runner_mod.artifact_read("default", "a1", "stdout", 0, 7)
                self.assertEqual(read["content"], "one\ntwo")
                tail = runner_mod.artifact_tail("default", "a1", "stdout", 2)
                self.assertEqual(tail["content"], "three\nfour")

    def test_timeout_kills_process_group(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = self.cfg(root)
            sessions = runner_mod.Sessions(cfg)
            runr = runner_mod.Runner(cfg, sessions)
            with mock.patch.object(runner_mod, "ARTIFACT_DIR", root / "artifacts"):
                result = runr.execute({
                    "session": "default",
                    "job_id": "timeout",
                    "op": "shell",
                    "cwd": str(root),
                    "command": "sleep 30",
                    "timeout": 1,
                })
            self.assertTrue(result["timed_out"])
            self.assertEqual(result["exit_code"], 124)

    def test_sqlite_wal_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "jobs.sqlite3"
            with mock.patch.object(state_mod, "STATE_DB", db), mock.patch.object(common, "STATE_DB", db):
                state_mod.state_put("default", "x1", {
                    "state": "done", "payload_hash": "abc", "result": {"status": "ok"}
                })
                got = state_mod.state_get("default", "x1")
                self.assertEqual(got["state"], "done")
                self.assertEqual(got["payload_hash"], "abc")
                self.assertEqual(got["result"]["status"], "ok")
                conn = state_mod.db_connect()
                try:
                    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                finally:
                    conn.close()
                self.assertEqual(str(mode).lower(), "wal")

    def test_legacy_json_state_migrates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "jobs.sqlite3"
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "one.json").write_text(json.dumps({
                "session": "default", "job_id": "old-1", "state": "done",
                "payload_hash": "hash", "result": {"status": "ok"}
            }), encoding="utf-8")
            with mock.patch.object(state_mod, "STATE_DB", db), mock.patch.object(common, "STATE_DB", db), \
                 mock.patch.object(state_mod, "LEGACY_STATE_DIR", legacy), mock.patch.object(common, "LEGACY_STATE_DIR", legacy):
                self.assertEqual(state_mod.migrate_legacy_state(), 1)
                self.assertEqual(state_mod.state_get("default", "old-1")["result"]["status"], "ok")

    def test_immutable_delete_refuses_changed_blob(self):
        with mock.patch.object(github_mod, "gh_get_meta", return_value={"sha": "new-sha"}), \
             mock.patch.object(github_mod, "run") as run_mock:
            with self.assertRaises(common.QueueMutationConflict):
                github_mod.gh_delete_immutable("owner/repo", "relay/jobs/default--x.json", "old-sha", "consume")
            run_mock.assert_not_called()

    def test_immutable_delete_uses_selected_sha(self):
        completed = type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with mock.patch.object(github_mod, "gh_get_meta", return_value={"sha": "same-sha"}), \
             mock.patch.object(github_mod, "run", return_value=completed) as run_mock:
            self.assertTrue(github_mod.gh_delete_immutable("owner/repo", "relay/jobs/default--x.json", "same-sha", "consume"))
            args = run_mock.call_args.args[0]
            self.assertIn("sha=same-sha", args)

    def test_job_schema_requires_timestamp_by_default(self):
        schema = common.job_schema(self.cfg("/tmp"))
        self.assertIn("created_unix", schema["required"])
        self.assertIn("artifact_read", schema["properties"]["op"]["enum"])

    def test_path_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.cfg(td)
            inside = Path(td) / "x"
            inside.write_text("ok")
            self.assertEqual(common.ensure_allowed(str(inside), cfg, exists=True), inside.resolve())
            with self.assertRaises(PermissionError):
                common.ensure_allowed("/etc/passwd", cfg, exists=True)


if __name__ == "__main__":
    unittest.main()
