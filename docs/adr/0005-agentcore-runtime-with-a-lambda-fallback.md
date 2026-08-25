# ADR-0005: Deploy on AgentCore Runtime, behind an HTTP contract that keeps Lambda one redeploy away

Date: 2026-08-25. Status: accepted (go/no-go gate was Sep 4; taken early on evidence).

## Context

The agent runs today as a local CLI. It must be deployable, observable, and
reachable by hackathon judges, and the rubric scores real use of AWS
services. Two candidate paths were researched against AWS documentation
rather than assumption:

- **A.** Amazon Bedrock AgentCore Runtime.
- **B.** The same Strands agent in a Lambda container with ADOT tracing.

Three findings decided it, and one of them removed the argument usually
made for AgentCore.

**The 15-minute ceiling is identical.** AgentCore's synchronous request
timeout is 900 seconds, exactly Lambda's. "Lambda times out" is not a
discriminator. What AgentCore adds is 8-hour sessions and stop/resume
against a stable `runtimeSessionId`.

**The human-in-the-loop pause does not discriminate either.** On both
paths the compute is ephemeral, so an approval that arrives hours later
requires persisting Strands interrupt state externally and re-invoking.
That is the same work on A and on B. Strands documents the mechanism
(`session_manager` persists interrupt state across teardown, and
`S3SessionManager` is the distributed option), and interrupts propagate
out of a `strands.multiagent` Graph, which is our topology.

**What actually differs is the integration path and the rubric surface.**
AgentCore has a documented Strands entrypoint (`BedrockAgentCoreApp` +
`@app.entrypoint`, with `agentcore create --framework Strands`
scaffolding), has been GA since October 2025, and supports every primitive
we need in us-east-1. Path A puts Runtime, Identity, and Observability on
the board where Path B puts Lambda and hand-baked ADOT (the managed ADOT
layers do not work with container images at all).

Cost is not a driver: roughly $0.47/month versus $1.50/month at 50 runs a
day, both rounding errors.

## Decision

Deploy on **AgentCore Runtime**, and build the agent to the runtime's HTTP
contract (`POST /invocations`, `GET /ping`, port 8080, arm64) rather than
to any platform-specific handler. Behind that contract the same image runs
under a Lambda Web Adapter, so a failed AgentCore deploy in the final week
costs a redeploy rather than a rewrite.

Specific choices inside that:

- **Persist interrupt state with `S3SessionManager`, not AgentCore session
  storage.** Session storage is Preview, expires after 14 idle days, and
  *resets on version update*, which would silently destroy in-flight
  approvals on any redeploy during the hackathon.
- **Stop and resume sessions; never hold one open.** Holding a session
  across a four-hour human wait bills memory for the whole wait, about
  240x the cost of stopping and resuming.
- **Keep the JSONL/S3 audit trail as the system of record.** AgentCore
  Memory is LLM-extracted semantic recall, not a legal record.
- **Skip Gateway, Code Interpreter, and Browser.** Our tools are already
  typed and in-process; adding a primitive for the sake of naming it would
  be surface, not depth.

## Consequences

- **A credential-free front door is now required work, not glue.**
  `InvokeAgentRuntime` accepts only IAM SigV4 or an OAuth 2.0 bearer
  token, so a judge cannot reach the agent directly. A thin public
  endpoint must hold the credential and invoke the runtime server-side.
  This is the single largest item the decision creates, and it is exactly
  the judge's-door surface the submission needs anyway.
- **The graph shape freezes once approvals are in flight.** Resuming
  against changed graph topology raises, so node renames stop being safe
  after the first persisted interrupt.
- **arm64 only, 2 GB image ceiling** (against Lambda's 10 GB). Mitigated by
  deploying `CodeZip` rather than a container, which also removes the
  cross-build from the critical path.
- **Cold start is unmeasured.** No latency figures are published for
  either service. Measure before the demo and pre-warm a pinned session;
  a judge's first click must not pay an unknown init cost.
- **Toolchain risk to retire early.** The `agentcore` CLI needs Node 20+,
  Python 3.10+, and CDK. This machine has a known node@25 failure, with
  node@22 prepended to PATH. Verify the CLI runs there before relying on
  it.

## The spike that must happen first

Every documented AgentCore + Strands example returns `result.message` from
a simple `Agent`. None shows a `Graph` with interrupts. Before any
deployment work, spike exactly one thing: deploy a runtime, force an
interrupt, and confirm `@app.entrypoint` surfaces the Strands interrupt
rather than swallowing it, then resume it from a second invocation. If
that fails, the same image behind a Lambda Web Adapter is the fallback and
the contract makes the switch cheap.
