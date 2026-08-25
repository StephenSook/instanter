# Spike 0001: does a Strands Graph interrupt survive AgentCore Runtime?

Date: 2026-08-25. Status: complete. Verdict: **yes, on both persistence paths.**

ADR-0005 chose AgentCore Runtime and named one thing that had to be proven before any
deployment work. Every documented AgentCore and Strands example returns
`result.message` from a simple `Agent`. None shows a `strands.multiagent` Graph that
suspends for a human decision, which is the whole shape of this product: the sweep
pauses before committing escalations, an attorney answers, and the run continues.

The spike is the smallest program that answers it. A two node graph, a gated tool, a
`BeforeToolCallEvent` hook that calls `event.interrupt(...)`, and an entrypoint that has
to carry the suspension out to the caller and take an answer back in on a later call.
Source is in `spikes/agentcore_interrupt/`.

## What was run

Deployed to AgentCore Runtime in `us-east-1` as a CodeZip runtime on `PYTHON_3_13`,
built with the `@aws/agentcore` CLI (0.28.0), invoked with `InvokeAgentRuntime`.

| Probe | Result |
|---|---|
| Interrupt surfaces through `@app.entrypoint` | Yes. `Status.INTERRUPTED`, the interrupt id, its name and the whole `reason` dict arrive intact |
| Resume on a **different** `runtimeSessionId` (cold microVM), state in our own S3 document | `COMPLETED`, hook received `approve`, gated node replied `DONE` |
| Resume on a **different** `runtimeSessionId`, state via `S3SessionManager` | `COMPLETED`, hook received `defer: reviewing tomorrow`, gated node replied `DEFERRED` |
| Negative control: resume with no session manager and no restore | Raises `ValueError: Received interrupt responses but agent is not in interrupt state` |
| First node re-executed on resume? | No. Its original reply is restored, so this is a real resume rather than a silent re-run |

The negative control is the part that gives the other rows meaning. Without it, two
green resumes would not have shown whether the persistence was load bearing or whether
the graph was quietly starting over.

## Latency, measured

AWS publishes no cold start figure for AgentCore Runtime, so it was measured.

| | Wall clock | In-container process uptime at first call |
|---|---|---|
| Cold (first call into a fresh runtime session) | 4.08s, 5.07s across two runs | 0.18s, 0.66s |
| Warm (same runtime session) | 0.15s to 0.18s | n/a |

Almost all of the cold cost is microVM provisioning rather than our own import time.
A pinned session should be pre-warmed before any demo so a first click does not pay it.

## Three traps worth carrying forward

**1. Returning a `GraphResult` from an entrypoint silently destroys the interrupt.**
`BedrockAgentCoreApp` falls back to `str()` on any value it cannot JSON encode. Handing
it the result object produced a 253 byte JSON *string* containing a Python `repr` of the
interrupts list. The status field was gone, the completed nodes were gone, and the word
"interrupt" never appeared as a status. Nothing raised and nothing warned, and the blob
still looks like data to a caller. The entrypoint must build its own response dict.

**2. The CLI silently dropped an `environment` map from `agentcore.json`.** It passed
`agentcore deploy --dry-run` validation, and the container still read an empty value.
Environment variables reach the runtime through the control plane
(`update-agent-runtime --environment-variables`); a value the runtime depends on should
also be asserted at startup rather than defaulted to empty.

**3. Because of (2), an early version returned HTTP 200 with a valid interrupt while
persisting nothing.** The guard read `if interrupted and BUCKET`, so an unset bucket
turned durability off instead of turning the run red. A pause that saves no state is
worse than a failure, because the caller is told a human can still answer. The guard now
raises, and reads the written object back before reporting success. Making that failure
loud is what surfaced the next one.

**4. The generated execution role has no access to an arbitrary S3 bucket.** With the
guard fixed, the run failed on `AccessDenied` for `s3:PutObject`. Any bucket the agent
persists to needs an explicit grant on the runtime's execution role. Under the original
silent guard this was invisible.

## Consequences for the build

- The human in the loop design in ADR-0005 stands, and now rests on a measurement rather
  than an inference. Interrupt state persists externally and resumes on fresh compute.
- `runtimeSessionId` has a **33 character minimum**, confirmed by trying 32 and 33. A
  short identifier fails parameter validation before the request leaves the client.
- Graph topology must stay stable once an approval is in flight, since resume rebuilds
  the same graph and restores state into it.
- Every bucket the deployed agent touches needs an execution role grant, and startup
  should refuse to run without the configuration it depends on.

## Reproducing

`spikes/agentcore_interrupt/main.py` is the runtime; `probe_deployed.py` drives the
deployed runtime and prints the verdict block. The spike keeps its own dependency list
and is deliberately outside the product's dependency closure.
