#!/usr/bin/env bash
set -euo pipefail

SOURCE_REPO="${CHATGPT_RELAY_SOURCE_REPO:-dmorazasanchez/ChatGPT-relay}"
SOURCE_REF="${CHATGPT_RELAY_REF:-main}"
SOURCE_BASE="https://raw.githubusercontent.com/${SOURCE_REPO}/${SOURCE_REF}"
EXPECTED_VERSION="1.2.1"
APP="chatgpt-relay"
BIN_DIR="$HOME/.local/bin"
CFG_DIR="$HOME/.config/$APP"
DATA_DIR="$HOME/.local/share/$APP"
APP_DIR="$DATA_DIR/app"
PKG_DIR="$APP_DIR/chatgpt_relay"
UNIT_DIR="$HOME/.config/systemd/user"
BIN="$BIN_DIR/$APP"
SELFTEST="$BIN_DIR/$APP-self-test"
SERVICE="$UNIT_DIR/$APP.service"
WATCHDOG="$BIN_DIR/$APP-watchdog"
WATCHDOG_SERVICE="$UNIT_DIR/$APP-watchdog.service"
WATCHDOG_TIMER="$UNIT_DIR/$APP-watchdog.timer"

usage() {
  cat <<'EOF'
Install ChatGPT Relay v1.2 on a Linux host.

Usage:
  install.sh --repo OWNER/PRIVATE_QUEUE_REPO --root /workspace [--root /other] [options]

Required:
  --repo OWNER/REPO       Dedicated GitHub queue repository. Private is strongly recommended.
  --root PATH             Workspace root. Repeat for multiple roots.

Options:
  --session NAME          Independent job lane. Repeatable. Default: default
  --queue-prefix PREFIX   Queue directory in the repo. Default: relay
  --http-port PORT        Local health/API port. Default: 8765
  --max-timeout SECONDS   Maximum shell timeout. Default: 1800
  --default-job-ttl SEC   Default GitHub job/control TTL. Default: 3600
  --max-job-ttl SEC       Maximum accepted TTL. Default: 86400
  --allow-untimestamped-jobs
                          Compatibility mode; disables required created_unix
  --max-job-bytes BYTES   Maximum GitHub queue payload size. Default: 1000000
  --skip-self-test        Do not submit the GitHub round-trip ping after installation
  -h, --help              Show this help

The GitHub queue contains no reusable relay secret. Write access to the queue repository
is the authorization boundary. The separate local HTTP API token stays on this host.
EOF
}

REPO=""
QUEUE_PREFIX="relay"
HTTP_PORT=8765
MAX_TIMEOUT=1800
DEFAULT_JOB_TTL=3600
MAX_JOB_TTL=86400
MAX_JOB_BYTES=1000000
ALLOW_UNTIMESTAMPED=0
ROOTS=()
SESSIONS=()
SKIP_SELF_TEST=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="${2:-}"; shift 2 ;;
    --root) ROOTS+=("${2:-}"); shift 2 ;;
    --session) SESSIONS+=("${2:-}"); shift 2 ;;
    --queue-prefix) QUEUE_PREFIX="${2:-}"; shift 2 ;;
    --http-port) HTTP_PORT="${2:-}"; shift 2 ;;
    --max-timeout) MAX_TIMEOUT="${2:-}"; shift 2 ;;
    --default-job-ttl) DEFAULT_JOB_TTL="${2:-}"; shift 2 ;;
    --max-job-ttl) MAX_JOB_TTL="${2:-}"; shift 2 ;;
    --max-job-bytes) MAX_JOB_BYTES="${2:-}"; shift 2 ;;
    --allow-untimestamped-jobs) ALLOW_UNTIMESTAMPED=1; shift ;;
    --skip-self-test) SKIP_SELF_TEST=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$REPO" ]] || { echo "--repo is required" >&2; exit 2; }
[[ ${#ROOTS[@]} -gt 0 ]] || { echo "At least one --root is required" >&2; exit 2; }
for n in "$HTTP_PORT" "$MAX_TIMEOUT" "$DEFAULT_JOB_TTL" "$MAX_JOB_TTL" "$MAX_JOB_BYTES"; do
  [[ "$n" =~ ^[0-9]+$ ]] || { echo "Numeric option has invalid value: $n" >&2; exit 2; }
done

for cmd in python3 curl gh systemctl base64; do
  command -v "$cmd" >/dev/null || { echo "Missing dependency: $cmd" >&2; exit 1; }
done
gh auth status >/dev/null 2>&1 || { echo "Authenticate GitHub first: gh auth login" >&2; exit 1; }
gh api "repos/$REPO" >/dev/null || { echo "Cannot access queue repository: $REPO" >&2; exit 1; }

mkdir -p "$BIN_DIR" "$CFG_DIR" "$DATA_DIR" "$APP_DIR" "$PKG_DIR" "$UNIT_DIR"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

FILES=(
  relay.py
  chatgpt_relay/__init__.py
  chatgpt_relay/common.py
  chatgpt_relay/state.py
  chatgpt_relay/github.py
  chatgpt_relay/runner.py
  chatgpt_relay/transport.py
  chatgpt_relay/server.py
  chatgpt_relay/cli.py
)
for rel in "${FILES[@]}"; do
  mkdir -p "$TMP_DIR/$(dirname "$rel")"
  curl -fsSL "$SOURCE_BASE/$rel" -o "$TMP_DIR/$rel"
done
python3 -m compileall -q "$TMP_DIR/relay.py" "$TMP_DIR/chatgpt_relay"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR"
cp -a "$TMP_DIR/relay.py" "$TMP_DIR/chatgpt_relay" "$APP_DIR/"
chmod 0755 "$APP_DIR/relay.py"

cat > "$BIN" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
exec python3 "$HOME/.local/share/chatgpt-relay/app/relay.py" "$@"
EOF
chmod 0755 "$BIN"

curl -fsSL "$SOURCE_BASE/self-test.sh" -o "$SELFTEST"
chmod +x "$SELFTEST"

INIT_ARGS=(init --repo "$REPO" --queue-prefix "$QUEUE_PREFIX" --http-port "$HTTP_PORT" --max-timeout "$MAX_TIMEOUT" --default-job-ttl "$DEFAULT_JOB_TTL" --max-job-ttl "$MAX_JOB_TTL" --max-job-bytes "$MAX_JOB_BYTES")
if [[ "$ALLOW_UNTIMESTAMPED" == 1 ]]; then INIT_ARGS+=(--allow-untimestamped-jobs); fi
for root in "${ROOTS[@]}"; do INIT_ARGS+=(--root "$root"); done
if [[ ${#SESSIONS[@]} -eq 0 ]]; then SESSIONS=(default); fi
for session in "${SESSIONS[@]}"; do INIT_ARGS+=(--session "$session"); done
"$BIN" "${INIT_ARGS[@]}" >/dev/null

ensure_keep() {
  local rel="$1"
  local path="$QUEUE_PREFIX/$rel/.keep"
  if ! gh api "repos/$REPO/contents/$path" >/dev/null 2>&1; then
    local content
    content=$(printf '{}\n' | base64 | tr -d '\n')
    gh api --method PUT "repos/$REPO/contents/$path" \
      -f message="chatgpt-relay bootstrap $rel" \
      -f content="$content" >/dev/null
  fi
}
ensure_keep jobs
ensure_keep control

cat > "$SERVICE" <<'EOF'
[Unit]
Description=ChatGPT Relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/bin/chatgpt-relay run
Restart=always
RestartSec=3
KillMode=control-group
TimeoutStopSec=8

[Install]
WantedBy=default.target
EOF

cat > "$WATCHDOG" <<EOF
#!/usr/bin/env bash
set -u
STATE="\$HOME/.local/share/chatgpt-relay/watchdog-failures"
body=\$(curl -sS --max-time 5 http://127.0.0.1:${HTTP_PORT}/health 2>/dev/null || true)
read_status=\$(printf '%s' "\$body" | python3 -c 'import json,sys
try:
 d=json.load(sys.stdin); print(("1" if d.get("ok") else "0")+" "+str(len(d.get("active") or [])))
except Exception: print("0 0")')
set -- \$read_status
ok=\${1:-0}; active=\${2:-0}
if [[ "\$ok" == 1 ]]; then echo 0 > "\$STATE"; exit 0; fi
if [[ "\$active" -gt 0 ]]; then exit 0; fi
n=0; [[ -f "\$STATE" ]] && n=\$(cat "\$STATE" 2>/dev/null || echo 0)
n=\$((n+1)); echo "\$n" > "\$STATE"
if [[ "\$n" -ge 3 ]]; then
  echo 0 > "\$STATE"
  systemctl --user restart chatgpt-relay.service
fi
EOF
chmod +x "$WATCHDOG"

cat > "$WATCHDOG_SERVICE" <<'EOF'
[Unit]
Description=ChatGPT Relay health watchdog
After=chatgpt-relay.service

[Service]
Type=oneshot
ExecStart=%h/.local/bin/chatgpt-relay-watchdog
EOF

cat > "$WATCHDOG_TIMER" <<'EOF'
[Unit]
Description=Check ChatGPT Relay every minute

[Timer]
OnBootSec=90
OnUnitActiveSec=60
AccuracySec=10
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now chatgpt-relay.service >/dev/null
systemctl --user enable --now chatgpt-relay-watchdog.timer >/dev/null

healthy=0
for _ in {1..30}; do
  if curl -fsS "http://127.0.0.1:${HTTP_PORT}/health" \
      | python3 -c "import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get('ok') and d.get('relay_version')=='$EXPECTED_VERSION' else 1)" \
      >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 1
done
if [[ "$healthy" != 1 ]]; then
  echo "Relay did not become healthy." >&2
  journalctl --user -u chatgpt-relay.service -n 100 --no-pager >&2 || true
  exit 1
fi

manifest_ok=0
for _ in {1..20}; do
  if gh api "repos/$REPO/contents/$QUEUE_PREFIX/status/capabilities.json" -H 'Accept: application/vnd.github.raw' 2>/dev/null \
      | python3 -c "import json,sys; d=json.load(sys.stdin); r=d.get('reliability',{}); raise SystemExit(0 if d.get('relay_version')=='$EXPECTED_VERSION' and r.get('immutable_queue_blobs') and r.get('serialized_github_mutations') else 1)" \
      >/dev/null 2>&1; then
    manifest_ok=1
    break
  fi
  sleep 1
done
[[ "$manifest_ok" == 1 ]] || { echo "Relay is healthy but v1.2 capabilities were not published." >&2; exit 1; }

if [[ "$SKIP_SELF_TEST" == 0 ]]; then "$SELFTEST" "${SESSIONS[0]}"; fi

LOCAL_TOKEN=$($BIN show-local-token)
cat <<EOF

ChatGPT Relay v1.2 is installed and healthy.

Queue repo:   $REPO
Queue prefix: $QUEUE_PREFIX
Sessions:     ${SESSIONS[*]}
Health:       http://127.0.0.1:$HTTP_PORT/health
Config:       $HOME/.config/chatgpt-relay/config.json
State DB:     $HOME/.local/share/chatgpt-relay/jobs.sqlite3

The GitHub transport requires NO relay secret in ChatGPT. Give ChatGPT access to the
dedicated queue repository and paste the bootstrap instructions from:
  $SOURCE_BASE/examples/CHATGPT_INSTRUCTIONS.md

Local HTTP API token (localhost only):
  $LOCAL_TOKEN

Security warning: a writer to $REPO can execute relay jobs as your Unix user.
Keep the queue repository private and dedicated to this relay.
EOF
