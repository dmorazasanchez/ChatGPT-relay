# ChatGPT Relay

A self-hosted GitHub-backed execution relay for ChatGPT.

It lets a ChatGPT conversation submit bounded jobs to **your own Linux machine** by writing small JSON files to a GitHub repository you control. A daemon on the machine polls that queue, executes the job, and writes the result back to GitHub for ChatGPT to read.

This is an **unofficial community project** and is not affiliated with OpenAI or GitHub.

## What it looks like

```text
ChatGPT
   |
   | GitHub app / write action
   v
private queue repository
   |
   | relay/jobs/default--123.json
   v
chatgpt-relay daemon on your Linux machine
   |
   | shell / files / git / builds / tests
   v
local workspace
   |
   | relay/results/default--123.json
   v
private queue repository -> ChatGPT
```

No inbound port, public IP, SSH server, or webhook is required. The Linux host only needs outbound access to GitHub.

## Important compatibility note

ChatGPT must have a GitHub integration/experience that can **write repository contents**, not only search/read them. App capabilities and write-action availability can vary by ChatGPT plan, workspace, and configuration. If your ChatGPT GitHub connection is read-only, it can inspect relay state but cannot submit jobs.

## Features

- GitHub file queue: no inbound network access required.
- Dedicated private queue repository recommended.
- No relay secret is stored in GitHub job history.
- Fixed `status/queue.json` index so ChatGPT does not need directory/code-search discovery.
- Durable local job ledger with conservative at-most-once recovery.
- One running job per session; multiple sessions can run concurrently.
- Process-group timeouts and cancellation.
- Full stdout/stderr retained locally; compact output returned through GitHub.
- Malformed JSON jobs are quarantined instead of blocking the queue.
- Session controls: pause, resume, stop, notes and priority text.
- Local-only HTTP health/API endpoint.
- systemd user service + watchdog.
- End-to-end GitHub round-trip self-test.

## Security model

**A writer to the queue repository can execute commands as the Unix account running the relay.**

Use a dedicated **private** queue repository and grant write access only to identities/apps you trust. `allowed_roots` restricts built-in file operations and the working directory of shell jobs; it is **not a shell sandbox**. A shell command can still access anything the Unix user can access. See [SECURITY.md](SECURITY.md).

## Requirements

On the Linux host:

- Python 3.10+
- `curl`
- GitHub CLI (`gh`)
- `systemd --user`
- `bash`
- `git` for the Git operations

Authenticate `gh` first:

```bash
gh auth login
```

## 1. Create a private queue repository

Use a separate repository from this public source repo. For example:

```bash
gh repo create my-chatgpt-relay-queue --private
```

The queue repository contains commands and results, so private is strongly recommended.

## 2. Install the relay

Choose the directories ChatGPT is allowed to work from:

```bash
curl -fsSL https://raw.githubusercontent.com/dmorazasanchez/ChatGPT-relay/main/install.sh | \
  bash -s -- \
  --repo YOUR_GITHUB_USER/my-chatgpt-relay-queue \
  --root "$HOME/projects"
```

Multiple roots are supported:

```bash
curl -fsSL https://raw.githubusercontent.com/dmorazasanchez/ChatGPT-relay/main/install.sh | \
  bash -s -- \
  --repo YOUR_GITHUB_USER/my-chatgpt-relay-queue \
  --root "$HOME/project-a" \
  --root "$HOME/project-b"
```

For independent concurrent work lanes, add sessions:

```bash
... --session code --session research --session build
```

If no session is supplied, the relay creates `default`.

The installer:

1. validates GitHub access,
2. installs `relay.py` to `~/.local/bin/chatgpt-relay`,
3. writes `~/.config/chatgpt-relay/config.json`,
4. creates the queue directories,
5. installs a systemd user service and watchdog,
6. verifies `/health`,
7. verifies `status/queue.json`,
8. submits a real GitHub `ping` job and validates the result.

## 3. Connect GitHub to ChatGPT

Connect GitHub in ChatGPT and grant it access to the **private queue repository**. The exact UI and action availability can vary by plan/workspace.

Then give ChatGPT the bootstrap instructions in [examples/CHATGPT_INSTRUCTIONS.md](examples/CHATGPT_INSTRUCTIONS.md). There is **no GitHub relay token to paste into ChatGPT**.

A short bootstrap prompt is:

```text
Use OWNER/QUEUE_REPO as my ChatGPT Relay queue.
Protocol: CHATGPT_RELAY_V1.
Read relay/status/hello.json and relay/status/queue.json first.
Use the job/result protocol documented in examples/CHATGPT_INSTRUCTIONS.md from dmorazasanchez/ChatGPT-relay.
Use only the configured sessions and allowed roots reported by hello.json.
```

## Queue protocol

Default prefix: `relay/`

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
```

Example ping job:

```json
{
  "protocol": "CHATGPT_RELAY_V1",
  "session": "default",
  "job_id": "ping-001",
  "op": "ping"
}
```

Create it as:

```text
relay/jobs/default--ping-001.json
```

The result appears at exactly:

```text
relay/results/default--ping-001.json
```

Do not include `default--` inside `job_id`; the namespace belongs only in the filename.

## Supported operations

### `ping`

Checks the round trip.

### `shell`

```json
{
  "protocol": "CHATGPT_RELAY_V1",
  "session": "default",
  "job_id": "build-001",
  "op": "shell",
  "cwd": "/home/user/project",
  "command": "git status --short && make -j8",
  "timeout": 900
}
```

### `read_file`

Reads a bounded line range from an allowed path.

### `write_file`

Writes UTF-8 text to an allowed path.

### `list_files`

Lists a bounded tree under an allowed directory.

### `git_status`

Runs `git status --short --branch`.

### `git_diff`

Returns a bounded git diff. Set `"cached": true` for staged changes.

### `cancel`

Sends SIGTERM to the process group of a running job.

## Sessions and controls

Each session serializes its own jobs. Different sessions can execute concurrently.

Control actions are submitted to `relay/control/<session>--<id>.json`:

```json
{
  "protocol": "CHATGPT_RELAY_V1",
  "session": "default",
  "action": "PAUSE"
}
```

Supported actions:

- `PAUSE`
- `RESUME`
- `STOP` — also terminates the active job in that session
- `NOTE`
- `PRIORITY`
- `CLEAR_PRIORITY`

## Reliability model

The relay keeps a local durable state for every job.

- Before execution: state becomes `running`.
- After execution: the full result is cached locally as `done`.
- The result is written to GitHub and read back for verification.
- Only after verification is the job file deleted.
- A cached `done` result is republished after a transport/restart failure without rerunning the command.
- If the relay restarts while a job was recorded as `running`, it returns `interrupted_previous_relay_instance` and **does not automatically rerun the command**.

This favors at-most-once behavior. True exactly-once execution is impossible for arbitrary shell side effects without cooperation from the command itself.

## Health and logs

```bash
curl http://127.0.0.1:8765/health
systemctl --user status chatgpt-relay.service
journalctl --user -u chatgpt-relay.service -f
```

Full command output is stored under:

```text
~/.local/share/chatgpt-relay/artifacts/
```

The GitHub result contains the corresponding local artifact paths when output was truncated.

Run the end-to-end test again at any time:

```bash
~/.local/bin/chatgpt-relay-self-test default
```

## Local HTTP API

The HTTP API is bound to `127.0.0.1` by default and uses a separate token that never needs to go into GitHub.

```bash
TOKEN=$(chatgpt-relay show-local-token)
curl \
  -H "X-Relay-Token: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"session":"default","job_id":"local-ping","op":"ping"}' \
  http://127.0.0.1:8765/
```

Rotate that local-only token with:

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
systemctl --user daemon-reload
```

Configuration/state are intentionally left behind. Remove them manually only if you no longer need them:

```bash
rm -rf ~/.config/chatgpt-relay ~/.local/share/chatgpt-relay
```

## License

MIT. See [LICENSE](LICENSE).
