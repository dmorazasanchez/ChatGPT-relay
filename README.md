# ChatGPT Relay

A self-hosted GitHub-backed execution relay for ChatGPT.

It lets a ChatGPT conversation submit bounded jobs to **your own Linux machine** by writing small JSON files to a GitHub repository you control. A daemon on the machine polls that queue, executes the job locally, and writes the result back to GitHub for ChatGPT to read.

This is an **unofficial community project** and is not affiliated with OpenAI or GitHub.

## Architecture

```text
ChatGPT
   |
   | GitHub write action
   v
private queue repository
   |
   | relay/jobs/<session>--<job>.json
   v
chatgpt-relay daemon on your Linux machine
   |
   | shell / files / git / builds / tests
   v
local workspace
   |
   | relay/results/<session>--<job>.json
   v
private queue repository -> ChatGPT
```

No inbound port, public IP, SSH server, or webhook is required. The Linux host only needs outbound access to GitHub.

## v1.1 reliability hardening

v1.1 adds the reliability layer needed for unattended use:

- **Immutable queue blobs.** The relay executes the exact Git blob selected from the queue and will only delete the queue path if it still points to that same blob. An edited job is never silently consumed as if it were the original.
- **Streaming stdout/stderr.** Shell output is streamed directly to local artifact files while only a bounded head/tail is kept in RAM and returned through GitHub.
- **Artifact operations.** ChatGPT can use `artifact_info`, `artifact_read`, and `artifact_tail` to retrieve full local logs through another relay job.
- **Job TTL.** GitHub jobs and controls carry `created_unix` and `ttl_seconds`, preventing old queued commands from unexpectedly executing after a machine reconnects.
- **SQLite WAL ledger.** Durable at-most-once job state lives in `~/.local/share/chatgpt-relay/jobs.sqlite3`. Existing v1.0 JSON state is migrated automatically.
- **Capabilities and schemas.** The relay publishes `capabilities.json`, `job.schema.json`, and `control.schema.json` in the status directory.
- **Fault-oriented CI.** Tests cover large output, timeouts, path boundaries, SQLite recovery, legacy-state migration, TTL rejection, artifact access, and immutable-delete conflicts.

## Security model

**Write access to the queue repository is authorization to execute commands as the Unix account running the relay.**

Use a dedicated **private** queue repository and grant write access only to identities/apps you trust. `allowed_roots` constrains built-in file operations and the working directory of shell jobs; it is **not a shell sandbox**. See [SECURITY.md](SECURITY.md).

Do not put passwords, API keys, private keys, or other long-lived secrets in job JSON or shell command strings: Git history is durable even after a queue file is deleted.

## Requirements

On the Linux host:

- Python 3.10+
- `curl`
- GitHub CLI (`gh`)
- `systemd --user`
- `bash`
- `git` for Git operations

Authenticate first:

```bash
gh auth login
```

## 1. Create a private queue repository

```bash
gh repo create my-chatgpt-relay-queue --private
```

Keep the queue separate from your source repositories.

## 2. Install

```bash
curl -fsSL https://raw.githubusercontent.com/dmorazasanchez/ChatGPT-relay/main/install.sh | \
  bash -s -- \
  --repo YOUR_GITHUB_USER/my-chatgpt-relay-queue \
  --root "$HOME/projects"
```

Multiple roots and independent concurrent sessions are supported:

```bash
curl -fsSL https://raw.githubusercontent.com/dmorazasanchez/ChatGPT-relay/main/install.sh | \
  bash -s -- \
  --repo YOUR_GITHUB_USER/my-chatgpt-relay-queue \
  --root "$HOME/project-a" \
  --root "$HOME/project-b" \
  --session code \
  --session build
```

The installer downloads the modular app into:

```text
~/.local/share/chatgpt-relay/app/
```

and creates the stable launcher:

```text
~/.local/bin/chatgpt-relay
```

It then configures systemd, verifies health/capabilities, and submits a real GitHub round-trip ping.

## 3. Connect ChatGPT

Connect GitHub in ChatGPT and grant the conversation/app access to the **private queue repository**. The GitHub integration must expose a repository **write action**, not only search/read.

Give ChatGPT the instructions in [examples/CHATGPT_INSTRUCTIONS.md](examples/CHATGPT_INSTRUCTIONS.md).

A short bootstrap prompt is:

```text
Use OWNER/QUEUE_REPO as my ChatGPT Relay queue.
Protocol: CHATGPT_RELAY_V1.
Read relay/status/hello.json, capabilities.json, heartbeat.json and queue.json first.
Follow dmorazasanchez/ChatGPT-relay examples/CHATGPT_INSTRUCTIONS.md.
```

## Queue layout

```text
relay/
  jobs/
    <session>--<job_id>.json
  results/
    <session>--<job_id>.json
  control/
    <session>--<control_id>.json
  control-results/
    <session>--<control_id>.json
  status/
    hello.json
    heartbeat.json
    sessions.json
    queue.json
    capabilities.json
    job.schema.json
    control.schema.json
```

## Freshness / TTL

By default, GitHub jobs and controls require:

```json
{
  "created_unix": 1787520000,
  "ttl_seconds": 3600
}
```

**Do not copy that example timestamp literally.** ChatGPT should normally read `relay/status/heartbeat.json` and copy its current `unix` value into `created_unix` when creating a job. If the computer has been offline long enough that the heartbeat is stale, the resulting job expires instead of unexpectedly executing much later.

Defaults:

- default TTL: 3600 seconds
- maximum TTL: 86400 seconds
- allowed future clock skew: 300 seconds

Compatibility mode is available with installer option `--allow-untimestamped-jobs`, but the default is safer.

## Example shell job

Assume the latest heartbeat contains `"unix": 1787520000`:

```json
{
  "protocol": "CHATGPT_RELAY_V1",
  "session": "default",
  "job_id": "build-001",
  "op": "shell",
  "created_unix": 1787520000,
  "ttl_seconds": 3600,
  "cwd": "/home/user/project",
  "command": "git status --short && make -j8",
  "timeout": 900
}
```

Create it as:

```text
relay/jobs/default--build-001.json
```

The result appears at:

```text
relay/results/default--build-001.json
```

Inside JSON, `job_id` stays unprefixed.

## Supported operations

- `ping`
- `shell`
- `read_file`
- `write_file`
- `list_files`
- `git_status`
- `git_diff`
- `cancel`
- `control_status`
- `artifact_info`
- `artifact_read`
- `artifact_tail`

The authoritative list is always the machine's `relay/status/capabilities.json`.

### Reading a large build log

A shell result contains artifact references such as:

```json
{
  "stdout_artifact_ref": {
    "session": "default",
    "job_id": "build-001",
    "stream": "stdout"
  }
}
```

Then submit, with a fresh timestamp:

```json
{
  "protocol": "CHATGPT_RELAY_V1",
  "session": "default",
  "job_id": "tail-build-001",
  "op": "artifact_tail",
  "created_unix": 1787520000,
  "ttl_seconds": 3600,
  "target_session": "default",
  "target_job_id": "build-001",
  "stream": "stdout",
  "lines": 200
}
```

`artifact_read` supports byte offsets for paging through a large file.

## Reliability semantics

The relay favors conservative at-most-once execution:

1. Select the immutable Git blob SHA for a queue entry.
2. Read and validate that exact blob.
3. Record `running` in SQLite before command execution.
4. Execute locally; shell stdout/stderr stream to artifacts.
5. Persist the complete result as `done` in SQLite.
6. Publish the result to GitHub and read it back for verification.
7. Delete the job only if its current GitHub blob SHA still equals the selected SHA.
8. Mark the local record `published`.

If publication fails after execution, the cached result is republished without rerunning the command. If the daemon restarts with a job recorded as `running`, it reports `interrupted_previous_relay_instance` and does **not** automatically repeat arbitrary side effects.

True exactly-once execution is impossible for arbitrary shell commands without cooperation from the command itself.

## Health and logs

```bash
curl http://127.0.0.1:8765/health
systemctl --user status chatgpt-relay.service
journalctl --user -u chatgpt-relay.service -f
```

Artifacts:

```text
~/.local/share/chatgpt-relay/artifacts/
```

SQLite ledger:

```text
~/.local/share/chatgpt-relay/jobs.sqlite3
```

Run the end-to-end test again:

```bash
~/.local/bin/chatgpt-relay-self-test default
```

## Local HTTP API

The HTTP API binds to `127.0.0.1` by default and uses a separate local token:

```bash
TOKEN=$(chatgpt-relay show-local-token)
curl -H "X-Relay-Token: $TOKEN" http://127.0.0.1:8765/capabilities
```

Rotate it with:

```bash
chatgpt-relay rotate-local-token
systemctl --user restart chatgpt-relay.service
```

## Uninstall

```bash
systemctl --user disable --now chatgpt-relay.service chatgpt-relay-watchdog.timer
rm -f ~/.config/systemd/user/chatgpt-relay.service
rm -f ~/.config/systemd/user/chatgpt-relay-watchdog.service
rm -f ~/.config/systemd/user/chatgpt-relay-watchdog.timer
rm -f ~/.local/bin/chatgpt-relay ~/.local/bin/chatgpt-relay-self-test ~/.local/bin/chatgpt-relay-watchdog
rm -rf ~/.local/share/chatgpt-relay/app
systemctl --user daemon-reload
```

Persistent state/artifacts are intentionally not removed by the uninstall commands above.
