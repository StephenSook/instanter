# ADR-0004: One docket is one identity; one human decision is immutable

Date: 2026-08-25. Status: accepted.

## Context

Twenty-four adversarial review rounds against the agent layer found 71
defects. They were not 71 unrelated bugs. Stripped of detail, almost every
one that reached the catastrophic class (a real overdue case held or
dropped while the run exits 0) came from one of two failures:

**A docket wore two identities.** Clinic intake is hand-keyed from printed
summonses, so one docket arrives twice under two spellings: a trailing
space, an interior space, a case variant, a separator the summons prints
and the staff entry drops, or a letter-for-digit lookalike (`O` for `0`,
`S` for `5`, `G` for `6`, `D` for `0`, `T` for `1`). Attorney capacity is
rationed, so two identities for one docket consume two of the day's slots
and ration out a genuinely distinct case. Every instance of this presented
identically: `succeeded=True`, `refused=()`, exit 0, and a real tenant one
day from a default writ sitting in `held`.

**A recorded human decision was overwritten.** The attorney response is
single-use, so any later interrupt is answered by the runner's own
synthetic text. When that text was allowed to write the decision scalar, a
writer retrying a partially failed commit flipped `approved` to
`deferred`, and the recovery path keyed on that scalar stopped running.
The runner held everything needed to deliver an approved case and did not.

Five separate fixes to the first problem regressed, three of them in
consecutive rounds, because each fix was scoped to the channel that had
been reported rather than to the rule.

## Decision

**Identity is computed, not compared.** `_identity_key` derives one key per
visual identity: strip every non-alphanumeric, casefold, then fold
letter-for-digit lookalikes everywhere except the two-letter division,
the one zone of the format where a letter carries meaning. Rows whose keys
collide are all refused, by name, and none sweeps. The fold table and the
pattern that recognizes the format are derived from a single
`_CONFUSABLE_LETTERS` definition, because maintaining them as two lists
that must agree is what produced three consecutive regressions: every
divergence between them was a silent hole. A one-sided edit now fails at
import.

Where folding cannot resolve an ambiguity it must refuse instead of guess.
A digit typed where a division letter belongs (`26E000101`) is genuinely
ambiguous between divisions `ED` and `EO`; folding it either way recreates
the collision the exemption exists to prevent, so the row is refused.

**A recorded decision is immutable for the run.** Once an approval is
bound, the hook refuses and audits any re-answer; the synthetic text
answering a second interrupt can cancel a tool but never rewrite what the
human decided. Recovery keys on the bound approval, the immutable
obligation the report's parity already used, never on the mutable action
scalar.

**Deferral is a channel, not prose.** Only `defer` or `defer: <reason>`
defers. Free text after a bare `defer ` is not adjudicable: `defer
nothing, commit them all` once parsed as a clean deferral and exited green
with nothing committed. Anything that is neither an exact approval nor an
explicit deferral is invalid, which voids the exchange, delivers the sweep
as pending review, and reads red.

## Consequences

- Two spellings of one docket can never occupy two capacity slots. The
  guarantee is pinned by a sweep parameterized over the digit space (a
  base docket per year-digit pair, every lookalike, every non-division
  position), not over one example id. The previous sweep tested a single
  docket whose own digits limited it to two of ten lookalike letters, and
  three regressions hid behind that blind spot with the suite green.
- False contests are possible and are the safe direction: they surface as
  a loud refusal on a red run, which is the posture the whole loader takes.
- Accepted residuals, each failing red and none reachable by any shipped
  id shape: a four-digit-year keying loses the division exemption; a
  division typed with both characters as digits is not refused (no real
  division code has both letters in the fold set); prefixed keyings such
  as `DIS-26ED-00101` over-refuse.
- An operator can always reconstruct what the attorney actually said: the
  response is stored verbatim when bounded, and as an excerpt plus a
  content digest and length when not.
