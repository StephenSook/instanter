#!/usr/bin/env bash
# Outside-in proof that the paid AgentCore path completed a recent scheduled
# sweep. The receipt proves the model, persistence, and interrupt path reached
# its human boundary. This check neither starts another paid run nor fabricates
# an attorney decision. Audit-lock and push outcomes remain separate signals.
#
# Usage: check_core.sh [base_url] [maximum_age_seconds]
set -u

BASE="${1:-https://d2ew2t4uldglcr.cloudfront.net}"
MAX_AGE="${2:-108000}" # 30 hours
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
BODY="$TMP/awaiting.json"
ERR="$TMP/curl.err"

code=$(curl -sS --max-time 25 -o "$BODY" -w '%{http_code}' "$BASE/api/awaiting" 2>"$ERR")
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "CORE CHECK FAILED: curl exited $rc"
  sed 's/^/  /' "$ERR"
  exit 1
fi
if [ "$code" != "200" ]; then
  echo "CORE CHECK FAILED: /api/awaiting returned HTTP $code"
  exit 1
fi
if [ ! -s "$BODY" ]; then
  echo "CORE CHECK FAILED: /api/awaiting returned an empty body"
  exit 1
fi

python3 - "$BODY" "$MAX_AGE" <<'PY'
import json
from pathlib import Path
import sys
import time

path = Path(sys.argv[1])
try:
    max_age = int(sys.argv[2])
except ValueError as exc:
    raise SystemExit(f"CORE CHECK FAILED: maximum age is not an integer: {exc}")
if max_age <= 0:
    raise SystemExit("CORE CHECK FAILED: maximum age must be positive")

raw = path.read_bytes()
if len(raw) < 120:
    raise SystemExit(
        f"CORE CHECK FAILED: response is only {len(raw)} bytes; expected a count-only receipt"
    )
try:
    payload = json.loads(raw)
except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"CORE CHECK FAILED: response is not JSON: {exc}")

entries = payload.get("awaiting")
if not isinstance(entries, list):
    raise SystemExit("CORE CHECK FAILED: response has no awaiting list")

scheduled = []
for entry in entries:
    if not isinstance(entry, dict) or entry.get("origin") != "scheduled":
        continue
    created = entry.get("created_at")
    cases = entry.get("cases")
    if not isinstance(created, int) or not isinstance(cases, int):
        raise SystemExit("CORE CHECK FAILED: scheduled receipt has an invalid shape")
    scheduled.append((created, cases))

if not scheduled:
    raise SystemExit("CORE CHECK FAILED: no scheduled AgentCore receipt is visible")

created, cases = max(scheduled)
now = int(time.time())
age = now - created
if age < -300:
    raise SystemExit(f"CORE CHECK FAILED: newest receipt is {-age}s in the future")
if age > max_age:
    raise SystemExit(
        f"CORE CHECK FAILED: newest scheduled AgentCore receipt is {age}s old; "
        f"maximum is {max_age}s"
    )
if cases <= 0:
    raise SystemExit("CORE CHECK FAILED: newest scheduled sweep exposed no interrupted cases")

print(
    f"CORE CHECK PASSED: newest scheduled AgentCore receipt is {age}s old "
    f"with {cases} interrupted case(s)"
)
PY
