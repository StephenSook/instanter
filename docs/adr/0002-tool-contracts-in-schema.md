# ADR-0002: Tool contracts live in the tool JSON schema, not docstring prose

Date: 2026-08-24. Status: accepted.

## Context

The first live smoke of the three-node graph worked end to end but the
audit trail showed the analyst failing every observation submission (60 to
84 validation rejections per run, zero acceptances) and the writer needing
6 to 8 attempts per rationale. The submission tools typed their payload
parameter as `dict[str, Any]`, so the tool schema the model saw had no
inner shape at all; the field names and required markers existed only as
prose in the docstring, and the model invented its own field names.

## Decision

Every model-facing tool takes flat, named, typed parameters. Required
fields are required in the generated JSON schema; optional enrichment
fields default to None. The tool body still constructs the strict Pydantic
model (`ExtractedObservations`, `EscalationRationale`), so the custom
validators (the advice-language blocklist, the disposition echo, bounds)
remain a second line of defense, and every rejection is audited with the
concrete validation errors, not just a count.

We considered typing the parameter as the Pydantic model itself (the
Strands decorator expands it into a nested schema and validates before the
body runs) but the body then receives a plain dict while the annotation
claims a model, which lies to the type checker. Flat parameters keep the
schema visible, the types honest, and small models reliable.

## Consequences

Measured on identical seed and prompts: observation acceptance went from
0 of 84 attempts to 12 of 12 first-try, rationales from 8 attempts for 2
to 2 for 2, and total tool calls per run from 97 to 19. Three consecutive
live runs were identical after the change.
