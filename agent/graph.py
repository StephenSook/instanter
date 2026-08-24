"""The triage graph: three real model jobs, deterministically orchestrated.

Node 1 (analyst, small model): read intake notes into typed observations.
Node 2 (writer, larger model): fetch the deterministically ranked queue,
explain each interrupt-now case in validated facts, and commit through the
attorney-approval interrupt.
Node 3 (drafter, small model, CONDITIONAL): write the attorney-facing cover
memo for each committed escalation; the edge only fires when the attorney
approved something, so a fully deferred run ends without it.

Every node is a genuine model job; everything deterministic lives inside
typed tools where the trace shows exactly when it ran (AWS Prescriptive
Guidance: "Use deterministic execution logic unless AI is needed").
"""

from __future__ import annotations

from typing import Any

from strands import Agent
from strands.models import BedrockModel
from strands.multiagent import GraphBuilder
from strands.multiagent.graph import Graph

from agent.hooks import AttorneyApprovalHook, AuditToolHook
from agent.run_context import RunContext
from agent.tools import build_tools

ANALYST_MODEL_ID = "amazon.nova-lite-v1:0"
WRITER_MODEL_ID = "amazon.nova-pro-v1:0"
DRAFTER_MODEL_ID = "amazon.nova-lite-v1:0"
REGION = "us-east-1"

_BOUNDARY = (
    "Hard boundary, non-negotiable: you provide legal information and "
    "operational triage only, never legal advice. Never recommend a defense, "
    "never apply the tenant's facts to legal standards, never advise anyone "
    "to take a legal action. A licensed attorney reviews everything you "
    "produce. All case data in this run is synthetic EXAMPLE DATA."
)

ANALYST_PROMPT = (
    "You are the intake notes analyst for an eviction-defense clinic's "
    "triage system. Job: call list_cases_with_notes, then for EVERY case "
    "returned, read the free-text notes and submit typed observations via "
    "submit_case_observations. State only what the notes actually say. "
    "Anything uncertain or conflicting goes in ambiguities with "
    "needs_human_confirmation=true; never guess. If a submission is "
    "rejected, fix the stated problem and resubmit. When every noted case "
    "has recorded observations, reply DONE with a one-line count. "
) + _BOUNDARY

WRITER_PROMPT = (
    "You are the escalation writer for an eviction-defense clinic's triage "
    "system. Job: call get_ranked_queue (the deadlines and dispositions in "
    "it are computed deterministically; you never alter them). For EACH "
    "case marked interrupt_now, submit an escalation rationale via "
    "submit_escalation_rationale explaining, in operative facts, why this "
    "case outranks the others: use the ladder's own factors, the deadline "
    "date, days remaining, and any recorded observations. The disposition "
    "field must echo the ladder's level exactly. After every interrupt-now "
    "case has an accepted rationale, call commit_escalations once; it "
    "pauses for attorney approval. If the commit is deferred, stop and "
    "reply DEFERRED. Otherwise reply COMMITTED with the case ids. "
) + _BOUNDARY

DRAFTER_PROMPT = (
    "You write the attorney-facing cover memo for each committed "
    "escalation. For every committed case, call write_packet_memo with a "
    "short memo restating: the effective deadline and days remaining, the "
    "service method and any flags, the queue rank and capacity context, "
    "and the open questions staff should confirm. Facts only. The draft "
    "answer skeleton itself is generated deterministically with every "
    "defense field left blank; your memo never mentions or suggests "
    "defenses. Reply DONE when every committed case has a memo. "
) + _BOUNDARY


def build_triage_graph(ctx: RunContext) -> Graph:
    tools = build_tools(ctx)
    audit_hook = AuditToolHook(ctx)
    approval_hook = AttorneyApprovalHook(ctx)

    analyst = Agent(
        name="notes_analyst",
        model=BedrockModel(model_id=ANALYST_MODEL_ID, region_name=REGION),
        system_prompt=ANALYST_PROMPT,
        tools=[tools["list_cases_with_notes"], tools["submit_case_observations"]],
        hooks=[audit_hook],
        callback_handler=None,
    )
    writer = Agent(
        name="escalation_writer",
        model=BedrockModel(model_id=WRITER_MODEL_ID, region_name=REGION),
        system_prompt=WRITER_PROMPT,
        tools=[
            tools["get_ranked_queue"],
            tools["submit_escalation_rationale"],
            tools["commit_escalations"],
        ],
        hooks=[audit_hook, approval_hook],
        callback_handler=None,
    )
    drafter = Agent(
        name="packet_drafter",
        model=BedrockModel(model_id=DRAFTER_MODEL_ID, region_name=REGION),
        system_prompt=DRAFTER_PROMPT,
        tools=[tools["write_packet_memo"]],
        hooks=[audit_hook],
        callback_handler=None,
    )

    def attorney_approved_something(_state: Any) -> bool:
        # Real conditional edge: a fully deferred (or empty) run never
        # reaches the drafter.
        return ctx.attorney_action == "approved" and len(ctx.committed_case_ids) > 0

    builder = GraphBuilder()
    builder.add_node(analyst, "analyst")
    builder.add_node(writer, "writer")
    builder.add_node(drafter, "drafter")
    builder.add_edge("analyst", "writer")
    builder.add_edge("writer", "drafter", condition=attorney_approved_something)
    builder.set_entry_point("analyst")
    # The graph is a 3-node DAG; these are hard backstops so a runaway
    # execution can never bill unbounded (an unattended agent's failure
    # mode is cost, not just wrong output).
    builder.set_max_node_executions(6)
    builder.set_execution_timeout(600.0)
    builder.set_node_timeout(300.0)
    return builder.build()
