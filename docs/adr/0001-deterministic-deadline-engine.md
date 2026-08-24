# ADR-0001: Deadline math is deterministic Python; the model never computes dates

Date: 2026-08-24. Status: accepted.

## Context

Instanter triages Fulton County dispossessory intake by proximity to a
statutory answer deadline (O.C.G.A. 44-7-51(b): 7 days from service, with
O.C.G.A. 1-3-1(d)(3) counting and terminal-day rolls). A wrong deadline is
not a degraded answer; it is the failure the product exists to prevent. An
LLM asked to count days across weekends, state legal holidays, and county
courthouse closures will be right most of the time, which is the dangerous
kind of right.

## Decision

All deadline computation lives in `engine/`: pure Python, zero AWS or model
dependencies, frozen behind its test corpus. The engine fails closed: any
input it cannot interpret (unknown service method, missing service date,
malformed types, out-of-coverage dates) produces a refusal with a reason,
never a guess. Models do two jobs only, both validated: reading free-text
intake notes into typed observations, and explaining an escalation the
deterministic ladder already decided. This follows AWS's own guidance for
agent design: "Use deterministic execution logic unless AI is needed"
(AWS Prescriptive Guidance).

The engine runs INSIDE agent tools (`get_ranked_queue`), so the execution
trace shows exactly when deterministic computation happened relative to
model turns.

## Consequences

- The correctness story is testable: every statutory edge case is a pytest
  case with a citation, not a prompt hope.
- Jurisdiction growth is a data change (a new row in the rule table), not a
  prompt change.
- The model layer can be disabled entirely (`--mode deterministic`) and the
  sweep still runs end to end, which is also what CI exercises without AWS
  credentials.
