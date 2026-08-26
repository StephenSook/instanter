#!/usr/bin/env bash
# Verify the deployed judge's door, from outside, with no credentials.
#
# Every check runs on a BARE exit path. Nothing here is piped to tail, because
# a pipeline returns its LAST command's status and that has already hidden one
# failed deploy on this project.
#
# The checks that matter are not "did it return 200". They are:
#   * the console is reachable by a stranger with no AWS account
#   * /api/stats RECOMPUTES, so two calls report two different timestamps
#   * the headline number equals the rows it claims to summarise
#   * the Lambda Function URL, hit DIRECTLY, is REFUSED, which is the whole
#     point of the shared origin secret
#
# Usage: ./verify_door.sh            (reads the stack outputs itself)
#        ./verify_door.sh <domain>   (checks a domain you name)

set -uo pipefail

STACK="${STACK:-InstanterJudgeDoor}"
REGION="${AWS_REGION:-us-east-1}"
fails=0

note() { printf '\n%s\n' "$1"; }
pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }

if [ $# -ge 1 ]; then
  DOMAIN="$1"
  FUNCTION_URL=""
else
  DOMAIN=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='DoorUrl'].OutputValue" --output text)
  FUNCTION_URL=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='FunctionUrl'].OutputValue" --output text)
fi

if [ -z "$DOMAIN" ] || [ "$DOMAIN" = "None" ]; then
  echo "could not resolve the door URL from stack $STACK"
  exit 1
fi

echo "door:          $DOMAIN"
echo "function url:  ${FUNCTION_URL:-<not resolved>}"

note "1. the console loads for a stranger"
code=$(curl -s -o /tmp/door-index.html -w '%{http_code}' --max-time 30 "$DOMAIN/")
if [ "$code" = "200" ]; then pass "GET / -> 200"; else fail "GET / -> $code"; fi
if grep -q "<div id=\"root\"" /tmp/door-index.html; then
  pass "the page is the console, not an S3 error document"
else
  fail "the body does not look like the console"
fi

note "2. health"
code=$(curl -s -o /tmp/door-health.json -w '%{http_code}' --max-time 30 "$DOMAIN/api/health")
if [ "$code" = "200" ]; then pass "GET /api/health -> 200"; else fail "GET /api/health -> $code"; fi

note "3. /api/stats recomputes rather than returning a stored answer"
curl -s --max-time 30 "$DOMAIN/api/stats" > /tmp/door-stats-1.json
sleep 1
curl -s --max-time 30 "$DOMAIN/api/stats" > /tmp/door-stats-2.json
python3 - <<'PY'
import json, sys

fails = 0
def pas(m): print(f"  PASS  {m}")
def bad(m):
    global fails
    fails += 1
    print(f"  FAIL  {m}")

a = json.load(open("/tmp/door-stats-1.json"))
b = json.load(open("/tmp/door-stats-2.json"))

rolls = len(a["because_the_deadline_rolls"])
summons = len(a["because_the_summons_controls"])
head = a["headline"]["answer_deadlines_hand_counting_gets_wrong"]
if head == rolls + summons:
    pas(f"headline {head} equals its rows ({rolls} roll + {summons} summons)")
else:
    bad(f"headline {head} does NOT equal {rolls} + {summons}")

if a["computation"]["elapsed_ms"] > 0 and b["computation"]["elapsed_ms"] > 0:
    pas(f"both calls report a real duration ({a['computation']['elapsed_ms']} ms, "
        f"{b['computation']['elapsed_ms']} ms)")
else:
    bad("a call reported zero elapsed time, which suggests a cached answer")

if "O.C.G.A." in a["computation"]["citation"]:
    pas("the statute is cited")
else:
    bad("no statute cited")

for row in a["because_the_deadline_rolls"]:
    if row["hand_counted_weekday"] not in ("Saturday", "Sunday"):
        bad(f"{row['case_id']} claims a roll but hand counting lands on "
            f"{row['hand_counted_weekday']}")
        break
else:
    pas("every roll row really does land on a weekend by hand")

sys.exit(1 if fails else 0)
PY
if [ $? -ne 0 ]; then fails=$((fails + 1)); fi

note "4. the Function URL, hit DIRECTLY, is refused"
if [ -n "$FUNCTION_URL" ] && [ "$FUNCTION_URL" != "None" ]; then
  code=$(curl -s -o /tmp/door-direct.json -w '%{http_code}' --max-time 30 "${FUNCTION_URL%/}/api/stats")
  if [ "$code" = "403" ]; then
    pass "direct origin access -> 403 (the shared secret is doing its job)"
  else
    fail "direct origin access -> $code, expected 403"
  fi
else
  echo "  SKIP  no function url resolved"
fi

note "5. a deep link reaches the console rather than an S3 404"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$DOMAIN/some/deep/link")
if [ "$code" = "200" ]; then pass "SPA rewrite works"; else fail "deep link -> $code"; fi

note "6. Web Push public key is on the door"
code=$(curl -s -o /tmp/door-vapid.json -w '%{http_code}' --max-time 30 "$DOMAIN/api/push/vapid")
if [ "$code" = "200" ] && python3 -c 'import json; assert json.load(open("/tmp/door-vapid.json")).get("publicKey")'; then
  pass "GET /api/push/vapid returns a publicKey"
else
  fail "GET /api/push/vapid -> $code"
fi

note "7. the service worker and manifest are on the origin"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$DOMAIN/sw.js")
if [ "$code" = "200" ]; then pass "GET /sw.js -> 200"; else fail "GET /sw.js -> $code"; fi
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$DOMAIN/manifest.webmanifest")
if [ "$code" = "200" ]; then pass "GET /manifest.webmanifest -> 200"; else fail "GET /manifest.webmanifest -> $code"; fi

note "8. OCR: Nova transcribes the sample summons, the engine computes 2026-08-17"
SAMPLE="$(cd "$(dirname "$0")/.." && pwd)/web/public/sample-summons.jpg"
if [ ! -f "$SAMPLE" ]; then
  fail "sample summons missing at $SAMPLE"
else
  python3 - "$DOMAIN" "$SAMPLE" <<'PY'
import json, sys, base64, urllib.request, urllib.error
domain, path = sys.argv[1], sys.argv[2]
raw = base64.b64encode(open(path, "rb").read()).decode()
req = urllib.request.Request(
    f"{domain}/api/ocr",
    data=json.dumps({"image_b64": raw, "media_type": "image/jpeg"}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=90) as res:
        body = json.loads(res.read())
        status = res.status
except urllib.error.HTTPError as exc:
    body = json.loads(exc.read() or b"{}")
    status = exc.code
    print(f"  FAIL  POST /api/ocr -> {status} {body.get('error')}")
    sys.exit(1)
if body.get("computed_deadline") != "2026-08-17":
    print(f"  FAIL  OCR deadline {body.get('computed_deadline')!r}, expected 2026-08-17")
    sys.exit(1)
if body.get("extracted", {}).get("service_date") != "2026-08-08":
    print(f"  FAIL  OCR service_date {body.get('extracted')}")
    sys.exit(1)
print("  PASS  sample summons 2026-08-08 -> engine 2026-08-17")
sys.exit(0)
PY
  if [ $? -ne 0 ]; then fails=$((fails + 1)); fi
fi

note "----"
if [ "$fails" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
  exit 0
fi
echo "$fails CHECK(S) FAILED"
exit 1
