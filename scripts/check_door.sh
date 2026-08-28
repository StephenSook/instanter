#!/usr/bin/env bash
# Outside-in liveness check for the public judge door.
#
# The door is the submission's primary evidence and judging runs for weeks with
# nobody watching, so this runs on a schedule and must FAIL LOUDLY when the door
# is not serving what it claims to serve.
#
# Two rules shape every check below.
#
# 1. Assert CONTENT, never a status code. A WAF challenge, a CloudFront error
#    page, and a stale cached shell all return 200 happily. Each check therefore
#    requires a specific string AND a plausible minimum body size, so a short
#    error stub cannot pass as a healthy response.
# 2. Bare exit paths. No pipe anywhere an exit code is read, because a
#    pipeline's status is its LAST command's and `curl ... | grep` reports grep.
#
# Usage: check_door.sh [base_url]
set -u
BASE="${1:-https://d2ew2t4uldglcr.cloudfront.net}"
FAILS=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# fetch <name> <path> <min_bytes> <required string>...
fetch() {
  local name="$1" path="$2" min="$3"; shift 3
  local out="$TMP/$name" code
  code=$(curl -sS --max-time 25 -o "$out" -w '%{http_code}' "$BASE$path" 2>"$TMP/$name.err")
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "FAIL $name: curl exited $rc"; sed 's/^/      /' "$TMP/$name.err"; FAILS=$((FAILS+1)); return
  fi
  if [ "$code" != "200" ]; then
    echo "FAIL $name: HTTP $code"; FAILS=$((FAILS+1)); return
  fi
  local size; size=$(wc -c < "$out" | tr -d ' ')
  if [ "$size" -lt "$min" ]; then
    echo "FAIL $name: body is ${size}B, under the ${min}B floor (an error or challenge page)"
    head -c 200 "$out" | sed 's/^/      /'; echo; FAILS=$((FAILS+1)); return
  fi
  local missing=0 s
  for s in "$@"; do
    grep -qF -- "$s" "$out" || { echo "FAIL $name: response does not contain '$s'"; missing=1; }
  done
  if [ "$missing" -ne 0 ]; then FAILS=$((FAILS+1)); return; fi
  echo "ok   $name (${size}B)"
}

echo "Probing $BASE"

# Liveness. Static, no AWS calls behind it, so a failure here is the door itself.
fetch health /api/health 40 '"ok": true' '"service": "instanter-judge-door"'

# The headline, recomputed per request by the deterministic engine. These are the
# exact figures every judge-facing surface publishes, so this doubles as a
# claim-drift guard: if the corpus or the engine changes, the published claim has
# changed too and someone must decide that deliberately.
fetch stats /api/stats 400 \
  '"answer_deadlines_hand_counting_gets_wrong": 4' \
  '"of_deadlines_computed": 46' \
  '"deadlines_computed": 46'

# The console shell a judge actually lands on.
fetch judge /judge 500 '<div id="root">'

echo
if [ "$FAILS" -gt 0 ]; then
  echo "DOOR CHECK FAILED ($FAILS)"
  exit 1
fi
echo "DOOR CHECK PASSED"
