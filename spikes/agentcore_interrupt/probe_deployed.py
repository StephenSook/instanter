"""Probe the DEPLOYED AgentCore runtime.

Answers three things the local server could not:
1. Does the interrupt survive AgentCore Runtime itself?
2. Can it be resumed from a SEPARATE invocation on a DIFFERENT runtime session,
   which is the real scenario (an attorney answers hours later, after the session
   has been stopped and the microVM is gone)?
3. What is the cold start? AWS publishes no figure for either service.
"""

from __future__ import annotations

import json
import sys
import time
import uuid

import boto3

ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:741030561008:runtime/"
    "instanterspike_interrupt_spike-zcL7ZNEqRi"
)
client = boto3.client("bedrock-agentcore", region_name="us-east-1")


def session_id() -> str:
    # runtimeSessionId has a 33 character MINIMUM, which is easy to miss.
    return f"spike-{uuid.uuid4().hex}-{uuid.uuid4().hex[:8]}"


def call(payload: dict, rt_session: str) -> tuple[dict, float]:
    started = time.time()
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=ARN,
        runtimeSessionId=rt_session,
        payload=json.dumps(payload).encode("utf-8"),
        contentType="application/json",
        accept="application/json",
    )
    body = resp["response"].read()
    elapsed = time.time() - started
    try:
        return json.loads(body), elapsed
    except json.JSONDecodeError:
        return {"_raw": body.decode("utf-8", "replace")[:600]}, elapsed


def show(label: str, data: dict, elapsed: float) -> None:
    print(f"\n--- {label}  (wall {elapsed:.2f}s)")
    keep = (
        "status",
        "interrupted",
        "interrupt_count",
        "hook_fired",
        "human_answer_seen_by_hook",
        "process_uptime_s",
        "pid",
        "resume_mode",
        "node_replies",
        "error",
        "_raw",
    )
    for k in keep:
        if k in data:
            print(f"  {k}: {data[k]}")
    if data.get("interrupts"):
        print(f"  interrupt_id: {data['interrupts'][0]['id']}")
        print(f"  reason: {data['interrupts'][0]['reason']}")


def main() -> int:
    s1 = session_id()
    logical = f"deployed-{uuid.uuid4().hex[:8]}"

    # 1. Cold start: first call into a fresh runtime session.
    ping, e = call({"action": "ping"}, s1)
    show("1. PING (cold: first call, new runtime session)", ping, e)
    cold_wall = e
    cold_uptime = ping.get("process_uptime_s")

    # 2. Warm call on the same session, to separate init from invoke.
    ping2, e2 = call({"action": "ping"}, s1)
    show("2. PING (warm: same runtime session)", ping2, e2)

    # 3. Start a run; expect an interrupt.
    started, e3 = call({"action": "start", "session_id": logical, "resume_mode": "explicit"}, s1)
    show("3. START", started, e3)
    if not started.get("interrupted"):
        print("\nFAILED: the deployed runtime did not surface an interrupt.")
        return 1
    interrupt_id = started["interrupts"][0]["id"]

    # 4. THE DECISIVE TEST: resume on a DIFFERENT runtime session id, which is a
    #    different microVM, so nothing in memory carries over.
    s2 = session_id()
    resumed, e4 = call(
        {
            "action": "resume",
            "session_id": logical,
            "resume_mode": "explicit",
            "interrupt_id": interrupt_id,
            "response": "approve",
        },
        s2,
    )
    show("4. RESUME on a DIFFERENT runtime session (cold microVM)", resumed, e4)

    # 5. Same again through the Strands session manager rather than our own doc.
    logical2 = f"deployed-{uuid.uuid4().hex[:8]}"
    s3 = session_id()
    st2, e5 = call({"action": "start", "session_id": logical2, "resume_mode": "session"}, s3)
    show("5. START (session-manager mode)", st2, e5)
    ok5 = bool(st2.get("interrupted"))
    if ok5:
        s4 = session_id()
        rs2, e6 = call(
            {
                "action": "resume",
                "session_id": logical2,
                "resume_mode": "session",
                "interrupt_id": st2["interrupts"][0]["id"],
                "response": "defer: reviewing tomorrow",
            },
            s4,
        )
        show("6. RESUME session-manager mode, DEFER, different runtime session", rs2, e6)

    print("\n===== VERDICT =====")
    print(f"cold wall clock:      {cold_wall:.2f}s")
    print(f"warm wall clock:      {e2:.2f}s")
    print(f"process uptime at first call: {cold_uptime}s")
    print(f"interrupt survived AgentCore Runtime: {started.get('interrupted')}")
    print(f"cross-session resume (explicit S3 state): {resumed.get('status')}")
    print(f"  hook saw the human answer: {resumed.get('human_answer_seen_by_hook')!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
