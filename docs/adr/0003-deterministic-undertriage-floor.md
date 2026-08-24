# ADR-0003: The deterministic floor: undertriage must be structurally impossible

Date: 2026-08-24. Status: accepted.

## Context

Chaos testing the live graph (the analyst's notes tool forced to time out
on every call) produced a failure mode worse than a crash: in some runs the
writer agent, seeing degraded upstream context, simply never called its
tools, and the graph ended with status COMPLETED and zero escalations. No
error, no failed node, no signal; every urgent case silently held. For
this product that is undertriage, the catastrophic error class: a tenant
seven days from a default writ whom nobody called.

Strands graphs are fail-fast (any node failure halts execution), and agent
nodes are discretionary by nature. Neither property can be allowed to
decide whether the sweep happens.

## Decision

`run_live` enforces a deterministic floor after the graph returns. If no
attorney decision was recorded (the writer never reached the approval
interrupt, whatever the reason: a dead node, a model that declined to act,
a provider outage), the runner itself completes the sweep: computes the
ranked queue if missing, escalates every interrupt-now case with a
rationale clearly labeled `[MODEL DISABLED: templated rationale]`, and
executes the attorney's decision through the same commit tool the writer
uses. The backstop emits a `deterministic_backstop` audit event naming the
graph status and what was missing, so a floor-completed run is never
mistaken for a model-completed run.

The same floor function is the whole of `--mode deterministic`, so the
backstop path is exercised by CI on every push, not only when chaos
strikes.

## Consequences

- Model discretion can degrade enrichment (observations, rationale prose,
  memos) but can never degrade the sweep itself.
- The chaos eval (`chaos-notes-timeout-approve-2`) asserts the guarantee
  live: with the notes tool dead, the run still commits exactly the
  ground-truth escalations.
- An operator reading the audit trail can always distinguish the three run
  shapes: model-completed, floor-completed, and deferred.
