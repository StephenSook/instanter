# Instanter

A background triage agent for eviction-defense clinics. It watches a clinic's intake queue of Fulton County, Georgia dispossessory (eviction) cases, computes each tenant's statutory answer deadline in deterministic Python, ranks the queue by proximity to a default writ of possession, and interrupts a supervising attorney for only the cases that cross a capacity-aware escalation threshold. Built with the Strands Agents SDK on Amazon Bedrock AgentCore for the AWS Agents for Humans Hackathon.

> **Instanter provides legal information and deadline computation, not legal advice, and is operated under attorney supervision.** It drafts only and never files. It computes deadlines and never advises. A licensed attorney is the reviewer of every case it surfaces. All demo data is synthetic; the statutory rules are real and public (O.C.G.A. 44-7-51; 1-3-1(d)(3); 1-4-1). No organization is a partner in or endorser of this project.

## Status

Under active build for the Agents for Humans Hackathon (submission window Aug 10 to Sep 14, 2026). This README grows with the code; nothing is claimed here before it ships.

## Why

The seven-day answer window in a Georgia dispossessory case is unforgiving: if the tenant does not answer, the court "shall issue a writ of possession instanter" (O.C.G.A. 44-7-53(a)). In completed 2015 Fulton County dispossessory cases, 54 percent of tenants never answered (Federal Reserve Bank of Atlanta, CED Discussion Paper 04-16, 2016). The Atlanta Volunteer Lawyers Foundation reports nearly 40,000 evictions filed in Fulton County each year, with fewer than 2 percent of tenants represented (avlf.org, 2025). A walk-in clinic cannot watch every clock. Software can.

## Architecture

Coming with the build: architecture diagram, deployment guide, and a full statutory citation chain. The design principle, from AWS Prescriptive Guidance: "Use deterministic execution logic unless AI is needed." The deadline math is plain, tested Python. The model perceives (reads intake notes) and communicates (explains an escalation). A human decides.

## Repository layout

```
engine/    deterministic deadline engine: jurisdiction rule table, 2026 holiday calendars, day-count logic
agent/     the triage agent: typed tools, the attorney-approval interrupt, the ladder, the audit trail
evals/     live evaluation harness plus the recorded run CI gates on
seed/      synthetic intake, labelled EXAMPLE DATA in every record
tests/     statutory test corpus for the engine, plus the agent's chaos and contract suites
docs/      architecture decision records
scripts/   CI gates (AI-tone, secret scanning wrappers)
```

Further directories (bff, infra, console, mobile) land as their phases ship.

## Development

```
uv sync --group dev
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

## License

Apache-2.0. See [LICENSE](LICENSE).
