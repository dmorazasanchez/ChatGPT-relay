#!/usr/bin/env bash
set -euo pipefail

SESSION="${1:-default}"
CFG="$HOME/.config/chatgpt-relay/config.json"
[[ -f "$CFG" ]] || { echo "Missing config: $CFG" >&2; exit 1; }
command -v gh >/dev/null || { echo "Missing gh" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh is not authenticated" >&2; exit 1; }

readarray -t META < <(python3 - "$CFG" "$SESSION" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1]))
session=sys.argv[2]
if session not in cfg.get('sessions', ['default']):
    raise SystemExit(f'unknown session: {session}')
print(cfg['repo'])
print(cfg.get('queue_prefix','relay'))
PY
)
REPO="${META[0]}"
PREFIX="${META[1]}"
JOB_ID="selftest-$(date +%s)-$RANDOM"
JOB_PATH="$PREFIX/jobs/$SESSION--$JOB_ID.json"
RESULT_PATH="$PREFIX/results/$SESSION--$JOB_ID.json"

PAYLOAD=$(python3 - "$SESSION" "$JOB_ID" <<'PY'
import json,sys
print(json.dumps({
  'protocol':'CHATGPT_RELAY_V1',
  'session':sys.argv[1],
  'job_id':sys.argv[2],
  'op':'ping'
}, indent=2))
PY
)
CONTENT=$(printf '%s\n' "$PAYLOAD" | base64 | tr -d '\n')

echo "Submitting $SESSION/$JOB_ID ..."
gh api --method PUT "repos/$REPO/contents/$JOB_PATH" \
  -f message="chatgpt-relay self-test $SESSION $JOB_ID" \
  -f content="$CONTENT" >/dev/null

RESULT=""
for _ in {1..30}; do
  if RESULT=$(gh api "repos/$REPO/contents/$RESULT_PATH" -H 'Accept: application/vnd.github.raw' 2>/dev/null); then
    break
  fi
  sleep 1
done
[[ -n "$RESULT" ]] || { echo "Timed out waiting for $RESULT_PATH" >&2; exit 1; }

printf '%s\n' "$RESULT" | python3 - "$JOB_ID" "$SESSION" <<'PY'
import json,sys
jid,session=sys.argv[1],sys.argv[2]
d=json.load(sys.stdin)
assert d.get('protocol') == 'CHATGPT_RELAY_V1', d
assert d.get('relay_version') == '1.0.0', d
assert d.get('job_id') == jid, d
assert d.get('session') == session, d
assert d.get('status') == 'ok', d
assert (d.get('result') or {}).get('pong') is True, d
print(json.dumps(d, indent=2))
PY

echo "PASS: GitHub -> relay -> GitHub round trip succeeded."
