# Instanter

A background triage agent for eviction-defense clinics. It watches a clinic's intake queue of Fulton County, Georgia dispossessory (eviction) cases, computes each tenant's statutory answer deadline in deterministic Python, ranks the queue by proximity to a default writ of possession, and interrupts a supervising attorney for only the cases that cross a capacity-aware escalation threshold. Built with the Strands Agents SDK on Amazon Bedrock AgentCore for the AWS Agents for Humans Hackathon.

> **Instanter provides legal information and deadline computation, not legal advice, and is operated under attorney supervision.** It drafts only and never files. It computes deadlines and never advises. A licensed attorney is the reviewer of every case it surfaces. All demo data is synthetic; the statutory rules are real and public (O.C.G.A. 44-7-51; 1-3-1(d)(3); 1-4-1). No organization is a partner in or endorser of this project.

## See it running

**<https://d2ew2t4uldglcr.cloudfront.net>**. No account, no key, no login.

**<https://d2ew2t4uldglcr.cloudfront.net/api/stats>** recomputes every answer deadline in the corpus on each request and reports what it found. Nothing there is cached or stored, so the numbers below are measured while you read them:

> **4 of 46 answer deadlines in the corpus are ones counting seven days by hand gets wrong.**
> Three of them roll off a weekend under O.C.G.A. 1-3-1(d)(3). One is controlled by a summons stating a different date, which under O.C.G.A. 44-7-51(b) is the date that binds the tenant. A missed answer deadline in a dispossessory case is a default judgment, which is an eviction.

The endpoint returns both lists in full, so the headline can be checked by counting rows. `infra/verify_door.sh` runs that check, and four others, from outside with no credentials.

### Run the agent yourself

`POST /api/run` starts a real sweep of all 48 cases on Amazon Bedrock AgentCore. It stops at the attorney interrupt and returns the cases a human is being asked to approve. Answer it with `POST /api/run/{id}/decision`:

```sh
RUN=$(curl -s -X POST https://d2ew2t4uldglcr.cloudfront.net/api/run \
  -H 'Content-Type: application/json' -d '{"capacity":2}' | jq -r .run_id)

curl -s -X POST "https://d2ew2t4uldglcr.cloudfront.net/api/run/$RUN/decision" \
  -H 'Content-Type: application/json' -d '{"response":"approve"}' | jq .result
```

Three answers, three different outcomes, and the third is the one worth trying:

| You send | What happens |
|---|---|
| `approve` | both cases committed, run succeeds |
| `defer: <reason>` | nothing committed, the cases stay listed as owed |
| `aprove` (a typo) | **not read as a decision.** The deterministic floor commits the cases for later review and the run reports failure, because no human actually decided. |

Live runs are capped per day, because this endpoint spends money on a model. `/api/stats` is pure arithmetic and is never capped.

### On a phone

The mobile app is the **attorney's decision surface**, not a second console. It exists for the escalation that fires while the one person allowed to decide is in a hallway at the courthouse. Same public door, no second backend.

| | |
|---|---|
| **Android** | [`instanter.apk`](https://github.com/StephenSook/instanter/releases/latest) on the latest release. Signed with our own release key, so Android will ask you to allow the install |
| **iOS** | <https://testflight.apple.com/join/JqZ1wX25> (public TestFlight link) |

The APK is a release asset rather than a build-service URL on purpose: build-service links expire in days while the repo, CI and deployment all stay green, so the download rots while every check still reports success.

## Status

Under active build for the Agents for Humans Hackathon (submission window Aug 10 to Sep 14, 2026). This README grows with the code; nothing is claimed here before it ships.

Shipped and reachable today: the deterministic deadline engine, the triage agent with its attorney-approval interrupt on AgentCore Runtime, the operator console, and the public door above.

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
infra/     the public judge door: CDK app, the door Lambda, and its outside-in verification
spikes/    throwaway experiments kept for their findings, not their code
web/       the operator console (React, Vite, Tailwind), deployed behind CloudFront
```

Further directories (bff, mobile) land as their phases ship.

## Development

```
uv sync --group dev
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

## License

Apache-2.0. See [LICENSE](LICENSE).
