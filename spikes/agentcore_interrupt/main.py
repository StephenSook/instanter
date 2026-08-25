"""Spike: does a Strands Graph interrupt survive Bedrock AgentCore Runtime?

ADR-0005 left exactly one question open. Every documented AgentCore + Strands
example returns ``result.message`` from a simple ``Agent``; none shows a
``strands.multiagent`` Graph that suspends on a human approval. This app is the
smallest thing that answers it: a two-node graph whose second node calls a gated
tool, a hook that interrupts before that tool runs, and an entrypoint that must
carry the interrupt OUT to the caller and take an answer back IN on a later,
separate invocation.

Four actions, each answering something specific:

* ``ping``    - liveness plus process uptime, which is how cold start is measured.
* ``start``   - run until the interrupt; report it as explicit JSON.
* ``resume``  - rebuild the graph in a fresh call and answer the interrupt, either
                through the session manager (``resume_mode: session``) or through a
                state document we wrote ourselves (``resume_mode: explicit``).
* ``raw``     - deliberately return the GraphResult object itself. The SDK falls
                back to ``str()`` on anything it cannot JSON-encode, so this probe
                shows whether a careless entrypoint silently flattens an interrupt
                into prose instead of failing loudly.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import boto3
from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent, tool
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry
from strands.models import BedrockModel
from strands.multiagent import GraphBuilder, Status
from strands.session.s3_session_manager import S3SessionManager
from strands.types.interrupt import InterruptResponseContent

PROCESS_START = time.time()
REGION = os.environ.get("AWS_REGION", "us-east-1")
# Deliberately no default. An S3 bucket name is account specific, and a spike that
# silently falls back to someone else's bucket name is worse than one that refuses to
# start. The runtime reads this from its environment; see the README for how to set it
# on a deployed runtime, which is not through agentcore.json.
BUCKET = os.environ.get("SPIKE_BUCKET", "")
MODEL_ID = os.environ.get("SPIKE_MODEL_ID", "amazon.nova-lite-v1:0")
COMMITTER_MODEL_ID = os.environ.get("SPIKE_COMMITTER_MODEL_ID", "amazon.nova-pro-v1:0")
GATED_TOOL = "commit_item"
STATE_PREFIX = "spike-explicit-state"
TASK = "Work item spike-001 is pending. Record it in the ledger."

app = BedrockAgentCoreApp()


@tool
def commit_item(item_id: str) -> str:
    """Commit one item to the ledger.

    Args:
        item_id: identifier of the item to commit.
    """
    return f"committed {item_id}"


class ApprovalHook(HookProvider):
    """Suspend before the gated tool runs so a human decides."""

    def __init__(self) -> None:
        self.answer: str | None = None
        self.fired = 0

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._gate)

    def _gate(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use["name"] != GATED_TOOL:
            return
        self.fired += 1
        tool_input = event.tool_use.get("input") or {}
        response = event.interrupt(
            "spike-approval",
            reason={
                "question": "Approve committing this item? Reply exactly 'approve'.",
                "item_id": tool_input.get("item_id"),
            },
        )
        self.answer = str(response)
        if self.answer.strip().lower() != "approve":
            event.cancel_tool = f"DEFERRED by the human: {self.answer}"


def build_graph(session_id: str, *, with_session_manager: bool) -> tuple[Any, ApprovalHook]:
    """Build the same graph shape on every invocation.

    Resuming against changed topology raises, so this function is the single
    definition both the start call and the resume call go through.
    """
    hook = ApprovalHook()
    model = BedrockModel(model_id=MODEL_ID, region_name=REGION)
    lister = Agent(
        name="lister",
        model=model,
        system_prompt=(
            "You are the intake step of a work queue. Reply with exactly this "
            "one line and nothing else: ITEM: spike-001"
        ),
        callback_handler=None,
    )
    committer = Agent(
        name="committer",
        model=BedrockModel(model_id=COMMITTER_MODEL_ID, region_name=REGION),
        system_prompt=(
            "You record work items. Your only action is to call the commit_item "
            "tool exactly once, with item_id set to the id named in the "
            "conversation, then reply DONE. Call the tool immediately; do not "
            "ask permission and do not explain first. If the tool comes back "
            "refused, reply DEFERRED and stop; never retry it."
        ),
        tools=[commit_item],
        hooks=[hook],
        callback_handler=None,
    )
    builder = GraphBuilder()
    builder.add_node(lister, "lister")
    builder.add_node(committer, "committer")
    builder.add_edge("lister", "committer")
    builder.set_entry_point("lister")
    builder.set_max_node_executions(6)
    builder.set_execution_timeout(300.0)
    builder.set_node_timeout(120.0)
    if with_session_manager and BUCKET:
        builder.set_session_manager(
            S3SessionManager(
                session_id=session_id,
                bucket=BUCKET,
                prefix="spike-sessions",
                region_name=REGION,
            )
        )
    return builder.build(), hook


def _s3() -> Any:
    return boto3.client("s3", region_name=REGION)


def save_explicit_state(session_id: str, graph: Any) -> str:
    """Write our own copy of the graph state, independent of Strands sessions."""
    key = f"{STATE_PREFIX}/{session_id}.json"
    _s3().put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(graph.serialize_state()).encode("utf-8"),
        ContentType="application/json",
    )
    return key


def load_explicit_state(session_id: str) -> dict[str, Any]:
    key = f"{STATE_PREFIX}/{session_id}.json"
    body = _s3().get_object(Bucket=BUCKET, Key=key)["Body"].read()
    return json.loads(body)


def _node_ids(value: Any) -> Any:
    """GraphResult carries node counts or node objects depending on the field."""
    if isinstance(value, int):
        return value
    return sorted(getattr(n, "node_id", str(n)) for n in value or [])


def describe(result: Any, hook: ApprovalHook) -> dict[str, Any]:
    """Build the response ourselves, so nothing depends on SDK serialization."""
    raw_interrupts = getattr(result, "interrupts", None) or []
    interrupts = [
        {
            "id": getattr(i, "id", None),
            "name": getattr(i, "name", None),
            "reason": getattr(i, "reason", None),
        }
        for i in raw_interrupts
    ]
    return {
        "status": str(getattr(result, "status", "UNKNOWN")),
        "interrupted": getattr(result, "status", None) == Status.INTERRUPTED,
        "interrupt_count": len(interrupts),
        "interrupts": interrupts,
        "hook_fired": hook.fired,
        "human_answer_seen_by_hook": hook.answer,
        "completed_nodes": _node_ids(getattr(result, "completed_nodes", None)),
        "interrupted_nodes": _node_ids(getattr(result, "interrupted_nodes", None)),
        "execution_count": getattr(result, "execution_count", None),
        "node_replies": {
            str(k): str(v)[:240] for k, v in (getattr(result, "results", None) or {}).items()
        },
    }


@app.entrypoint
def invoke(payload: dict[str, Any], context: Any = None) -> Any:
    """The AgentCore Runtime entrypoint."""
    action = str(payload.get("action", "start"))
    session_id = str(payload.get("session_id") or "spike-default")
    resume_mode = str(payload.get("resume_mode", "session"))
    started = time.time()
    base: dict[str, Any] = {
        "action": action,
        "session_id": session_id,
        "runtime_session_id": getattr(context, "session_id", None),
        "process_uptime_s": round(time.time() - PROCESS_START, 3),
        "pid": os.getpid(),
    }

    if action == "ping":
        return {**base, "ok": True, "bucket": BUCKET, "model": MODEL_ID}

    if action == "raw":
        graph, _hook = build_graph(session_id, with_session_manager=False)
        # No dict, no formatting: hand the SDK the object and see what arrives.
        return graph(TASK)

    use_session_manager = resume_mode == "session"
    graph, hook = build_graph(session_id, with_session_manager=use_session_manager)

    if action == "resume":
        if resume_mode == "explicit":
            graph.deserialize_state(load_explicit_state(session_id))
        interrupt_id = str(payload.get("interrupt_id", ""))
        answer = str(payload.get("response", ""))
        responses: list[InterruptResponseContent] = [
            {"interruptResponse": {"interruptId": interrupt_id, "response": answer}}
        ]
        result = graph(responses)
    else:
        result = graph(TASK)

    out = {**base, "resume_mode": resume_mode, **describe(result, hook)}
    if out["interrupted"]:
        if not BUCKET:
            raise RuntimeError(
                "REFUSING to report an interrupt with no durable state: SPIKE_BUCKET "
                "is empty, so the approval could never be resumed. A run that pauses "
                "for a human and persists nothing is worse than a run that fails."
            )
        key = save_explicit_state(session_id, graph)
        # Read it back. A put that reports success is not a state you can resume.
        size = _s3().head_object(Bucket=BUCKET, Key=key)["ContentLength"]
        out["explicit_state_key"] = key
        out["explicit_state_bytes"] = size
    out["elapsed_s"] = round(time.time() - started, 3)
    return out


if __name__ == "__main__":
    app.run()
