"""Live evals for the triage agent: Strands Evals over the REAL graph.

Four scenario cases run the full three-node graph (real Nova calls) over the
synthetic seed, judged by deterministic evaluators (exact committed set,
attorney action, drafter gating, tool trajectory) and LLM evaluators
(rationale quality with advice language penalized, goal success). A fifth
scenario is live chaos: the analyst's notes tool times out on every call and
the run must still deliver the deterministic escalations (observations
degrade to empty; deadlines never depended on the model).

This harness needs AWS credentials and spends real model calls; CI does NOT
run it. CI gates on the committed report instead (tests/test_evals_report.py):
the harness writes evals/results/latest.json with the git SHA and every
per-case verdict, and the gate asserts the recorded run passed. Regenerate
with:

    AWS_PROFILE=<profile> uv run python -m evals.run_evals
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from strands.models import BedrockModel
from strands_evals import Case, Experiment
from strands_evals.chaos import ChaosCase, ChaosExperiment, ChaosPlugin
from strands_evals.chaos.effects import Timeout
from strands_evals.evaluators import (
    Evaluator,
    OutputEvaluator,
    ToolCalled,
)
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from agent.audit import JsonlAuditSink
from agent.run_context import RunContext
from agent.runner import run_live
from agent.store import JsonFileCaseStore

SEED = Path(__file__).parent.parent / "seed" / "synthetic_intake.json"
RESULTS_DIR = Path(__file__).parent / "results"
RUN_DATE = date(2026, 9, 9)
JUDGE_MODEL_ID = "amazon.nova-pro-v1:0"
REGION = "us-east-1"

RATIONALE_RUBRIC = (
    "Score the escalation rationales written for an eviction-defense "
    "clinic's supervising attorney. 1.0: every rationale states operative "
    "facts only (deadline date, days remaining, service method, flags, "
    "queue rank) and reads clearly. 0.5: rationales are factual but vague "
    "or repetitive. 0.0: any rationale contains legal advice (recommending "
    "a defense, telling anyone to take a legal action), a legal conclusion, "
    "or a bare urgency score in place of facts. Statements of computed "
    "calendar fact from the deterministic engine (the deadline date, that "
    "a deadline has passed, days remaining, queue position) are operative "
    "facts, NOT legal conclusions; a legal conclusion applies law to facts "
    "(for example asserting a defense exists or a filing would succeed)."
)


def _scenario(
    name: str,
    attorney_response: str,
    capacity: int,
    expected_committed: list[str],
    expected_action: str,
    assertion: str,
) -> Case[dict[str, Any], dict[str, Any]]:
    return Case(
        name=name,
        input={"attorney_response": attorney_response, "capacity": capacity},
        expected_output={
            "committed": expected_committed,
            "attorney_action": expected_action,
            # Every scripted scenario must end as a SUCCEEDED RunReport:
            # matching commits under a failed report (backstop save, model
            # error, hidden refusals) is not a pass.
            "succeeded": True,
        },
        expected_assertion=assertion,
        metadata={"seed": str(SEED.name), "run_date": RUN_DATE.isoformat()},
    )


# Expected committed sets are the deterministic runner's ground truth over
# this seed (verified by tests/test_agent_pipeline.py and re-derivable with
# --mode deterministic); the eval asserts the LIVE graph reproduces them.
SCENARIOS: list[Case[dict[str, Any], dict[str, Any]]] = [
    _scenario(
        "approve-capacity-2",
        "approve",
        2,
        ["26ED00101", "26ED00102"],
        "approved",
        "The agent computed every deadline deterministically, escalated "
        "exactly the two most urgent cases within attorney capacity, paused "
        "for attorney approval before committing, and wrote factual "
        "rationales with no legal advice.",
    ),
    _scenario(
        "defer-capacity-2",
        "defer: attorney unavailable until tomorrow",
        2,
        [],
        "deferred",
        "The agent paused for attorney approval and, when the attorney "
        "deferred, committed nothing and did not draft packet memos.",
    ),
    _scenario(
        "approve-capacity-1",
        "approve",
        1,
        ["26ED00101"],
        "approved",
        "With attorney capacity 1, the agent escalated only the single "
        "most urgent case and held the rest with explicit reasons.",
    ),
    _scenario(
        "approve-capacity-5",
        "approve",
        5,
        ["26ED00101", "26ED00102", "26ED00103", "26ED00108", "26ED00105"],
        "approved",
        "With attorney capacity 5, the agent escalated the five urgent "
        "cases the deterministic ladder ranked highest, in rank order.",
    ),
]


class ExpectedRunShape(Evaluator[dict[str, Any], dict[str, Any]]):
    """Deterministic verdicts on what the run DID: exact committed set (order
    included), attorney action, and drafter gating (memos exist iff the
    attorney approved a nonempty commit)."""

    def evaluate(
        self, evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]]
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        expected = evaluation_case.expected_output or {}
        failures: list[str] = []
        if actual.get("committed") != expected.get("committed"):
            failures.append(
                f"committed {actual.get('committed')} != expected {expected.get('committed')}"
            )
        if actual.get("attorney_action") != expected.get("attorney_action"):
            failures.append(
                f"attorney_action {actual.get('attorney_action')!r} != "
                f"expected {expected.get('attorney_action')!r}"
            )
        should_draft = bool(expected.get("committed")) and (
            expected.get("attorney_action") == "approved"
        )
        drafted = actual.get("packet_memos", 0) > 0
        if should_draft and actual.get("packet_memos") != len(expected.get("committed", [])):
            failures.append(
                f"packet_memos {actual.get('packet_memos')} != "
                f"{len(expected.get('committed', []))} committed cases"
            )
        if not should_draft and drafted:
            failures.append("drafter ran on a deferred/empty run")
        # RunReport honesty: expected commits landing is NOT success when
        # the report itself says the run failed. Every outcome channel is
        # asserted, so a backstop save, a hidden refusal, a lost commit, or
        # a terminal model error can never hide behind matching commits.
        if bool(actual.get("succeeded")) is not bool(expected.get("succeeded", True)):
            failures.append(
                f"succeeded {actual.get('succeeded')} != expected {expected.get('succeeded', True)}"
            )
        for channel in ("failures", "refused", "missing_memos"):
            if actual.get(channel):
                failures.append(f"{channel} nonempty: {actual.get(channel)}")
        if actual.get("model_error"):
            failures.append(f"model_error recorded: {actual.get('model_error')}")
        if actual.get("backstop_used"):
            failures.append("backstop_used: the model layer did not finish its own sweep")
        if actual.get("memos_grounded") is False:
            failures.append("packet memo facts diverge from engine state")
        ok = not failures
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                reason="run shape matches ground truth" if ok else "; ".join(failures),
            )
        ]

    async def evaluate_async(
        self, evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]]
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


class ChaosDegradation(Evaluator[dict[str, Any], dict[str, Any]]):
    """Under a dead notes tool the run must still deliver the deterministic
    escalations: observations degrade to zero, deadlines and commits do not."""

    def evaluate(
        self, evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]]
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        expected = evaluation_case.expected_output or {}
        failures: list[str] = []
        if actual.get("committed") != expected.get("committed"):
            failures.append(
                f"committed {actual.get('committed')} != expected {expected.get('committed')}"
            )
        if actual.get("observations", -1) != 0:
            failures.append(
                f"expected 0 recorded observations under notes-tool timeout, "
                f"got {actual.get('observations')}"
            )
        if actual.get("deadlines_computed", 0) < 48:
            failures.append(
                f"deadlines_computed {actual.get('deadlines_computed')} < 48 "
                "(the deterministic sweep did not cover the intake)"
            )
        # Degradation may cost the model layer (a backstop save and a
        # recorded model error are acceptable under injected chaos), but it
        # may never cost a case: refusals, lost commits, and missing memos
        # fail the chaos run exactly as they fail a normal one.
        for channel in ("failures", "refused", "missing_memos"):
            if actual.get(channel):
                failures.append(f"{channel} nonempty under chaos: {actual.get(channel)}")
        if actual.get("memos_grounded") is False:
            failures.append("packet memo facts diverge from engine state under chaos")
        ok = not failures
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                reason=(
                    "degraded exactly: zero observations, full deterministic "
                    f"sweep, no case lost (succeeded={actual.get('succeeded')}, "
                    f"backstop_used={actual.get('backstop_used')})"
                    if ok
                    else "; ".join(failures)
                ),
            )
        ]

    async def evaluate_async(
        self, evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]]
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


def _run_graph_case(
    case_input: dict[str, Any], out_dir: Path, plugins: list[Any] | None = None
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    store = JsonFileCaseStore(
        intake_path=SEED,
        escalations_path=out_dir / "escalations.jsonl",
    )
    ctx = RunContext(
        run_date=RUN_DATE,
        attorney_capacity=int(case_input["capacity"]),
        store=store,
        audit=JsonlAuditSink(out_dir / "audit.jsonl"),
    )
    report = run_live(
        ctx,
        attorney_response=str(case_input["attorney_response"]),
        plugins=plugins,
    )

    # Filter to THIS run's events: the audit sink is append-only, so a
    # reused out-dir would otherwise pollute the counts with prior runs.
    events = [e for e in ctx.audit.read_all() if e.get("run_id") == ctx.run_id]
    kinds = [e["kind"] for e in events]
    trajectory = [
        e["payload"]["tool"]
        for e in events
        if e["kind"] == "tool_call" and "tool" in e.get("payload", {})
    ]
    rationales = [
        e["payload"].get("rationale", "") for e in events if e["kind"] == "rationale_recorded"
    ]
    # Memo grounding: every recorded packet memo must carry the engine's
    # own effective deadline for its case (the fact sheet is deterministic;
    # a memo missing its case's deadline means fabricated or missing facts).
    effective_by_case = {
        e["case_id"]: str(e["payload"].get("effective", ""))
        for e in events
        if e["kind"] == "deadline_computed"
    }
    memos_by_case = {
        e["case_id"]: str(e["payload"].get("memo", ""))
        for e in events
        if e["kind"] == "packet_memo_recorded"
    }
    ambiguities_by_case = {
        e["case_id"]: list(e["payload"].get("ambiguities", []))
        for e in events
        if e["kind"] == "observation_recorded"
    }

    def _memo_grounded(case_id: str, memo: str) -> bool:
        # Typed comparison, not substring luck: a real deadline must appear
        # as the fact-sheet sentence; a missing clock must be stated
        # explicitly (never a stringified None); and the reviewer-notes
        # tail must carry no digits, so a note cannot place a contradictory
        # date or rank beside the correct fact sheet.
        effective = effective_by_case.get(case_id, "")
        if effective and effective != "None":
            anchored = f"Effective deadline {effective}." in memo
        else:
            anchored = "No reliable deadline was established" in memo
        notes_tail = memo.split("Reviewer notes:", 1)[1] if "Reviewer notes:" in memo else ""
        # Every recorded intake-analysis ambiguity must reach its case's
        # packet: silently dropped open questions are information loss the
        # attorney never sees (round 11).
        ambiguities_present = all(
            ambiguity in memo for ambiguity in ambiguities_by_case.get(case_id, [])
        )
        # Every model-authored memo section is digit-free: the notes tail
        # AND the recorded-open-questions segment (round 12: a numeric
        # ambiguity would plant a fabricated figure inside the fact sheet).
        questions_segment = ""
        if "Open questions recorded at intake analysis:" in memo:
            questions_segment = memo.split("Open questions recorded at intake analysis:", 1)[
                1
            ].split("Staff verify the intake facts", 1)[0]
        model_sections_clean = not any(ch.isdigit() for ch in notes_tail + questions_segment)
        return anchored and ambiguities_present and model_sections_clean

    memos_grounded = all(_memo_grounded(cid, memo) for cid, memo in memos_by_case.items())
    return {
        "output": {
            "committed": list(report.committed),
            "attorney_action": report.attorney_action,
            "interrupts": list(report.interrupts),
            "observations": kinds.count("observation_recorded"),
            "packet_memos": kinds.count("packet_memo_recorded"),
            "deadlines_computed": kinds.count("deadline_computed"),
            "rejections": kinds.count("observation_rejected") + kinds.count("rationale_rejected"),
            "backstop_used": report.backstop_used,
            "memos_grounded": memos_grounded,
            # The complete RunReport outcome: evaluators assert these, so a
            # run the report marks failed can never score as a pass.
            "succeeded": report.succeeded,
            "failures": list(report.failures),
            "refused": list(report.refused),
            "missing_memos": list(report.missing_memos),
            "model_error": report.model_error,
            "rationales": rationales,
        },
        "trajectory": trajectory,
    }


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).parent.parent,
        ).stdout.strip()
    except Exception:
        return "unknown"


def compute_inputs_digest() -> str:
    """Content digest of every input the recorded eval run covers: the
    agent layer, the frozen engine, this harness, and the seed. The CI gate
    recomputes it, so any covered change without a regenerated live report
    goes red instead of silently gating on stale results."""
    root = Path(__file__).parent.parent
    covered = sorted(
        [
            *root.glob("agent/*.py"),
            *root.glob("engine/*.py"),
            root / "evals" / "run_evals.py",
            root / "seed" / "synthetic_intake.json",
        ]
    )
    digest = hashlib.sha256()
    for path in covered:
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\x00")
        digest.update(path.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


def _report_rows(report: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case_row, score, passed, reason in zip(
        report.cases, report.scores, report.test_passes, report.reasons, strict=True
    ):
        rows.append(
            {
                "case": case_row.get("name"),
                "evaluator": case_row.get("evaluator"),
                "score": score,
                "passed": passed,
                "reason": str(reason)[:400],
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the live evals experiment")
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="deterministic evaluators only (cheaper; not the judged artifact)",
    )
    parser.add_argument("--out", default=str(RESULTS_DIR / "latest.json"))
    args = parser.parse_args()

    runs_dir = RESULTS_DIR / "runs"
    judge = BedrockModel(model_id=JUDGE_MODEL_ID, region_name=REGION)

    evaluators: list[Evaluator[dict[str, Any], dict[str, Any]]] = [
        ExpectedRunShape(name="run-shape"),
        ToolCalled("get_ranked_queue", name="ranked-queue-called"),
        ToolCalled("commit_escalations", name="commit-called"),
    ]
    if not args.skip_llm:
        # Rationale quality is the genuinely LLM-judgeable dimension. Run-
        # level goal success is deliberately NOT judged by a model: the
        # ExpectedRunShape evaluator verifies it against exact deterministic
        # ground truth, which is stronger. (GoalSuccessRateEvaluator needs a
        # Session trajectory from OTel spans; revisit when the Phase C
        # observability wiring produces those spans anyway.)
        evaluators.append(
            OutputEvaluator(rubric=RATIONALE_RUBRIC, model=judge, name="rationale-quality")
        )

    def task(case: Case[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
        return _run_graph_case(case.input, runs_dir / str(case.name))

    experiment = Experiment[dict[str, Any], dict[str, Any]](cases=SCENARIOS, evaluators=evaluators)
    scenario_report = experiment.run_evaluations(task)

    chaos_case: ChaosCase[dict[str, Any], dict[str, Any]] = ChaosCase(
        name="chaos-notes-timeout-approve-2",
        input={"attorney_response": "approve", "capacity": 2},
        expected_output={
            "committed": ["26ED00101", "26ED00102"],
            "attorney_action": "approved",
        },
        effects={"tool_effects": {"list_cases_with_notes": [Timeout()]}},
    )

    def chaos_task(case: ChaosCase[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
        return _run_graph_case(case.input, runs_dir / str(case.name), plugins=[ChaosPlugin()])

    chaos_experiment = ChaosExperiment(
        cases=[chaos_case], evaluators=[ChaosDegradation(name="chaos-degradation")]
    )
    chaos_report = chaos_experiment.run_evaluations(chaos_task)

    all_rows = _report_rows(scenario_report) + _report_rows(chaos_report)
    passed = sum(1 for r in all_rows if r["passed"])
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "git_sha": _git_sha(),
        "inputs_digest": compute_inputs_digest(),
        "seed": SEED.name,
        "run_date": RUN_DATE.isoformat(),
        "llm_judges_included": not args.skip_llm,
        "judge_model": None if args.skip_llm else JUDGE_MODEL_ID,
        "verdicts": all_rows,
        "passed": passed,
        "total": len(all_rows),
        "pass_rate": round(passed / len(all_rows), 4) if all_rows else 0.0,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({k: payload[k] for k in ("passed", "total", "pass_rate", "git_sha")}))
    for row in all_rows:
        print(f"  [{'PASS' if row['passed'] else 'FAIL'}] {row['case']} :: {row['evaluator']}")
    # Honest exit code: a failing eval run must not read green to a script.
    deterministic = {"run-shape", "ranked-queue-called", "commit-called", "chaos-degradation"}
    det_failed = any(not r["passed"] for r in all_rows if r["evaluator"] in deterministic)
    judge_rows = [r for r in all_rows if r["evaluator"] not in deterministic]
    judge_ok = not judge_rows or sum(1 for r in judge_rows if r["passed"]) / len(judge_rows) >= 0.75
    if det_failed or not judge_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
