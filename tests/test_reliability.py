import base64
import base64
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
from chatgpt_relay import transport as transport_mod


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

    def test_transport_payload_accepts_json_and_base64_envelope(self):
        payload = self.fresh_job(
            op="shell",
            cwd="/tmp",
            command="printf 'a\\\\nb\\\\t[c]'",
        )
        raw = json.dumps(payload)
        obj, encoding = common.decode_transport_payload(raw)
        self.assertEqual(encoding, "json")
        self.assertEqual(obj["command"], payload["command"])

        envelope = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        obj2, encoding2 = common.decode_transport_payload(envelope)
        self.assertEqual(encoding2, "base64-json")
        self.assertEqual(obj2, payload)

    def test_transport_payload_invalid_still_rejected(self):
        bad = '{"protocol":"CHATGPT_RELAY_V1","command":"literal' + chr(10) + 'newline"}'
        with self.assertRaises(json.JSONDecodeError):
            common.decode_transport_payload(bad)

    def test_poll_jobs_accepts_base64_json_envelope(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.cfg(td)
            cfg.update(repo="owner/repo", queue_prefix="relay")
            sessions = runner_mod.Sessions(cfg)
            runr = runner_mod.Runner(cfg, sessions)
            transport = transport_mod.Transport(cfg, runr, sessions)
            job = self.fresh_job(job_id="env-ping")
            raw = json.dumps(job)
            envelope = base64.b64encode(raw.encode("utf-8")).decode("ascii")
            item = {"name": "default--env-ping.json", "sha": "a" * 40}
            with mock.patch.object(transport_mod, "gh_list", return_value=[item]),                  mock.patch.object(transport_mod, "gh_get_blob_text", return_value=envelope),                  mock.patch.object(transport, "_publish_queue_manifest"),                  mock.patch.object(transport_mod.threading.Thread, "start", autospec=True):
                launched = transport.poll_jobs()
            self.assertEqual(launched, 1)
            self.assertIn("default--env-ping.json", transport.inflight)
            self.assertIn("default", transport.claimed)

    def test_version_and_capabilities(self):
        self.assertEqual(common.VERSION, "1.2.2")
        caps = common.capabilities(self.cfg("/tmp"))
        self.assertTrue(caps["reliability"]["immutable_queue_blobs"])
        self.assertTrue(caps["reliability"]["streaming_stdout_stderr"])
        self.assertEqual(caps["reliability"]["durable_state"], "sqlite-wal")
        self.assertTrue(caps["reliability"]["serialized_github_mutations"])
        self.assertTrue(caps["reliability"]["github_retry_backoff"])
        self.assertTrue(caps["reliability"]["job_scoped_cancel"])
        self.assertTrue(caps["reliability"]["shell_command_b64"])
        self.assertIn("artifact_tail", caps["operations"])

    def test_safe_ids(self):
        self.assertEqual(common.normalize_id("abc-123_test.x", "id"), "abc-123_test.x")
        with self.assertRaises(ValueError):
            common.normalize_id("../bad", "id")

    def test_ttl_accepts_fresh_and_rejects_expired(self):
        cfg = self.cfg("/tmp")
        now = int(time.time())
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

    def test_shell_command_b64_preserves_backslashes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = self.cfg(root)
            sessions = runner_mod.Sessions(cfg)
            runr = runner_mod.Runner(cfg, sessions)
            command = "printf '%s\\n' 'a\\b'"
            encoded = base64.b64encode(command.encode()).decode()
            job = self.fresh_job(
                job_id="b64-shell", op="shell", cwd=str(root), command_b64=encoded, timeout=5
            )
            common.validate_job(cfg, job)
            with mock.patch.object(runner_mod, "ARTIFACT_DIR", root / "artifacts"):
                result = runr.execute(job)
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["stdout"], "a\\b\n")

    def test_shell_requires_exactly_one_command_encoding(self):
        cfg = self.cfg("/tmp")
        base = self.fresh_job(op="shell", cwd="/tmp")
        with self.assertRaises(ValueError):
            common.validate_job(cfg, base)
        with self.assertRaises(ValueError):
            common.validate_job(cfg, dict(base, command="true", command_b64="dHJ1ZQ=="))
        common.validate_job(cfg, dict(base, command="true"))
        common.validate_job(cfg, dict(base, command_b64="dHJ1ZQ=="))

    def test_job_scoped_cancel_refuses_wrong_source_blob(self):
        cfg = self.cfg("/tmp")
        sessions = runner_mod.Sessions(cfg)
        runr = runner_mod.Runner(cfg, sessions)
        proc = type("P", (), {"pid": 4242})()
        runr.active["default--build-7"] = {
            "process": proc,
            "session": "default",
            "job_id": "build-7",
            "source_blob_sha": "a" * 40,
        }
        with mock.patch.object(runner_mod.os, "killpg") as killpg:
            refused = runr.cancel("default", "build-7", "b" * 40)
            self.assertFalse(refused["cancelled"])
            self.assertEqual(refused["reason"], "source-blob-mismatch")
            killpg.assert_not_called()
            accepted = runr.cancel("default", "build-7", "a" * 40)
            self.assertTrue(accepted["cancelled"])
            killpg.assert_called_once_with(4242, runner_mod.signal.SIGTERM)

    def test_cancel_validation_accepts_optional_blob_sha(self):
        cfg = self.cfg("/tmp")
        job = self.fresh_job(
            job_id="cancel-1", op="cancel", target_job_id="build-7", target_source_blob_sha="a" * 40
        )
        common.validate_job(cfg, job)
        with self.assertRaises(ValueError):
            common.validate_job(cfg, dict(job, target_source_blob_sha="not-a-sha"))

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

    def test_github_writes_use_hardened_limits(self):
        self.assertEqual(github_mod.GH_READ_TIMEOUT, 12)
        self.assertEqual(github_mod.GH_WRITE_TIMEOUT, 15)
        self.assertGreaterEqual(github_mod.GH_RETRY_ATTEMPTS, 5)

    def test_job_schema_requires_timestamp_by_default(self):
        schema = common.job_schema(self.cfg("/tmp"))
        self.assertIn("created_unix", schema["required"])
        self.assertIn("artifact_read", schema["properties"]["op"]["enum"])
        self.assertIn("command_b64", schema["properties"])
        self.assertIn("target_source_blob_sha", schema["properties"])

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
