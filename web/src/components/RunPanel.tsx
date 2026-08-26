import { useEffect, useRef, useState } from "react";
import gsap from "gsap";
import {
  DoorError,
  decideRun,
  outcomeOf,
  receiptSteps,
  startRun,
  type AwaitingCase,
  type RunEnvelope,
  type RunResult,
} from "../data";

/** Ask the agent to sweep, then answer the attorney interrupt it stops at.
 *
 *  This is the product's central claim as an interaction. Everything on this
 *  panel comes from the door's own responses: no optimistic state, no invented
 *  case, no placeholder count. When a call fails, the failure is what renders.
 *
 *  The three answers are all reachable on purpose, including the unclear one.
 *  A near-miss reply is refused as a decision, the deterministic floor commits
 *  the cases for later review, and the run reports FAILURE. That behaviour is
 *  the argument for the whole design, so it is shown rather than smoothed away.
 */

type State =
  | { k: "idle" }
  | { k: "starting" }
  | { k: "awaiting"; runId: string; result: RunResult }
  | { k: "deciding"; runId: string; result: RunResult; answer: string }
  | { k: "resolved"; runId: string; result: RunResult }
  | { k: "capped"; detail: string; runsToday?: number; cap?: number }
  | { k: "error"; message: string };

const APPROVE = "approve";
const DEFER = "defer: reviewing with the supervising attorney";
// Deliberately a near miss. The door must not read it as a decision.
const UNCLEAR = "aprove";

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <section className="border-b border-white/10 bg-[var(--color-ground-soft)]">
      <div className="mx-auto max-w-[1400px] px-5 py-10 sm:px-10">{children}</div>
    </section>
  );
}

function Heading({ children }: { children: React.ReactNode }) {
  return (
    <p className="font-mono text-[0.66rem] tracking-[0.2em] text-white/55 uppercase">{children}</p>
  );
}

function Stamp({ label, tone }: { label: string; tone: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    gsap.fromTo(
      node,
      { scale: 2.1, opacity: 0, rotate: -14 },
      { scale: 1, opacity: 1, rotate: -6, duration: 0.32, ease: "power4.out" },
    );
  }, []);
  return (
    <div
      ref={ref}
      className="inline-block -rotate-6 rounded-[3px] border-[3px] px-4 py-1.5 font-mono text-[1.05rem] font-bold tracking-[0.2em] uppercase"
      style={{ color: tone, borderColor: tone }}
    >
      {label}
    </div>
  );
}

function CaseRow({ c }: { c: AwaitingCase }) {
  const overdue = c.days_remaining !== null && c.days_remaining < 0;
  return (
    <li
      data-desk-card
      className="paper-grain rounded-[3px] bg-[var(--color-paper)] p-4 text-[var(--color-ink)]"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-mono text-[0.95rem] font-semibold">{c.case_id}</span>
        <span
          className="font-mono text-[0.7rem] tracking-[0.12em] uppercase"
          style={{ color: overdue ? "var(--color-stamp)" : "var(--color-ink-soft)" }}
        >
          rank {c.rank} &middot;{" "}
          {c.days_remaining === null
            ? "no reliable clock"
            : overdue
              ? `${Math.abs(c.days_remaining)} day(s) overdue`
              : `${c.days_remaining} day(s) left`}
        </span>
      </div>
      {c.rationale && (
        <p className="mt-2 font-serif text-[0.95rem] leading-snug">{c.rationale}</p>
      )}
      {c.flags.length > 0 && (
        <p className="mt-2 font-mono text-[0.62rem] tracking-wide text-[var(--color-ink-soft)] uppercase">
          {c.flags.join(" &middot; ")}
        </p>
      )}
    </li>
  );
}

export function RunPanel() {
  const [state, setState] = useState<State>({ k: "idle" });
  const busy = state.k === "starting" || state.k === "deciding";

  function handleFailure(e: unknown) {
    if (e instanceof DoorError && e.status === 429) {
      setState({
        k: "capped",
        detail: e.message,
        runsToday: e.body?.runs_today as number | undefined,
        cap: e.body?.cap as number | undefined,
      });
      return;
    }
    setState({ k: "error", message: e instanceof Error ? e.message : String(e) });
  }

  async function begin() {
    if (busy) return; // one click, one run: this one spends model tokens
    setState({ k: "starting" });
    try {
      const env: RunEnvelope = await startRun(2);
      const result = env.result;
      if (!result) throw new Error("the door returned no result");
      if (result.interrupted) setState({ k: "awaiting", runId: env.run_id, result });
      else setState({ k: "resolved", runId: env.run_id, result });
    } catch (e) {
      handleFailure(e);
    }
  }

  async function answer(text: string) {
    if (state.k !== "awaiting" || busy) return;
    const { runId, result } = state;
    setState({ k: "deciding", runId, result, answer: text });
    try {
      const env = await decideRun(runId, text);
      if (!env.result) throw new Error("the door returned no result");
      setState({ k: "resolved", runId, result: env.result });
    } catch (e) {
      handleFailure(e);
    }
  }

  return (
    <Shell>
      {state.k === "idle" && (
        <div className="flex flex-wrap items-end justify-between gap-6">
          <div className="max-w-2xl">
            <Heading>The sweep</Heading>
            <h2 className="display mt-2 text-[clamp(1.8rem,4vw,3rem)] leading-none">
              Run it yourself
            </h2>
            <p className="mt-3 text-[0.95rem] leading-snug text-white/80">
              Every case is read, every answer deadline is computed from the statute, and the
              queue is ranked. Then the agent stops and asks a licensed human before it commits
              anything. You are the human.
            </p>
          </div>
          <button
            type="button"
            onClick={begin}
            className="rounded-[3px] bg-[var(--color-paper)] px-6 py-3.5 font-mono text-[0.78rem] font-semibold tracking-[0.18em] text-[var(--color-ink)] uppercase shadow-[0_3px_0_rgba(0,0,0,0.35)] transition-transform duration-200 hover:-translate-y-0.5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[var(--color-flag)]"
          >
            Sweep the queue
          </button>
        </div>
      )}

      {state.k === "starting" && (
        <div>
          <Heading>Working</Heading>
          <p className="mt-2 font-serif text-[1.15rem] text-white/80">
            Reading intake notes, computing every deadline from the statute, ranking the queue.
          </p>
          <p className="mt-1.5 font-mono text-[0.66rem] text-white/55">
            This is a real model run on Amazon Bedrock. It takes a few seconds.
          </p>
        </div>
      )}

      {(state.k === "awaiting" || state.k === "deciding") && (
        <div>
          <Heading>Stopped for the attorney</Heading>
          <h2 className="display mt-2 text-[clamp(1.6rem,3.4vw,2.6rem)] leading-none">
            {state.result.awaiting?.length ?? 0} case
            {(state.result.awaiting?.length ?? 0) === 1 ? "" : "s"} need a decision
          </h2>
          <p className="mt-2 font-mono text-[0.66rem] text-white/55">
            {state.result.total_cases} swept &middot; nothing is committed until you answer
          </p>

          <AttorneyDesk cases={state.result.awaiting ?? []} />
          <RunReceipt result={state.result} />

          <div className="mt-7 flex flex-wrap items-center gap-3">
            <button
              type="button"
              disabled={busy}
              onClick={() => answer(APPROVE)}
              className="rounded-[3px] border-2 border-[var(--color-stamp)] px-5 py-2.5 font-mono text-[0.78rem] font-bold tracking-[0.2em] text-[var(--color-stamp)] uppercase disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[var(--color-flag)]"
            >
              Approve
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => answer(DEFER)}
              className="rounded-[3px] border-2 border-white/60 px-5 py-2.5 font-mono text-[0.78rem] font-bold tracking-[0.2em] text-white uppercase disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[var(--color-flag)]"
            >
              Defer
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => answer(UNCLEAR)}
              className="font-mono text-[0.66rem] tracking-[0.12em] text-white/55 uppercase underline decoration-white/40 underline-offset-4 disabled:opacity-50 hover:text-white/80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[var(--color-flag)]"
            >
              Or send an unclear answer, and watch it refuse
            </button>
          </div>
          {state.k === "deciding" && (
            <p className="mt-4 font-mono text-[0.66rem] text-white/55">
              Recording the decision and resuming the run.
            </p>
          )}
        </div>
      )}

      {state.k === "resolved" && <Resolved runId={state.runId} result={state.result} />}

      {state.k === "capped" && (
        <div>
          <Heading>Daily limit reached</Heading>
          <p className="mt-2 max-w-2xl font-serif text-[1.05rem] leading-snug text-white/80">
            {state.detail}
          </p>
          {state.runsToday !== undefined && state.cap !== undefined && (
            <p className="mt-1.5 font-mono text-[0.66rem] text-white/55 tabular-nums">
              {state.runsToday} started today against a cap of {state.cap}
            </p>
          )}
        </div>
      )}

      {state.k === "error" && (
        <div>
          <Heading>The run did not start</Heading>
          <p className="mt-2 max-w-2xl font-serif text-[1.05rem] leading-snug text-white/80">
            {state.message}
          </p>
          <p className="mt-1.5 font-mono text-[0.66rem] text-white/55">
            Nothing is shown here that the door did not return.
          </p>
        </div>
      )}
    </Shell>
  );
}

function AttorneyDesk({ cases }: { cases: AwaitingCase[] }) {
  const reduced =
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const deskRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = deskRef.current;
    if (!node || reduced) return;
    const cards = node.querySelectorAll("[data-desk-card]");
    if (cards.length === 0) return;
    gsap.fromTo(
      cards,
      { y: 56, opacity: 0, rotate: -4 },
      { y: 0, opacity: 1, rotate: 0, duration: 0.45, stagger: 0.08, ease: "power4.out" },
    );
  }, [cases, reduced]);

  return (
    <section
      ref={deskRef}
      aria-label="Attorney desk"
      className="mt-6 rounded-[3px] border border-white/10 bg-[#2a241c] p-4 sm:p-5"
    >
      <p className="font-mono text-[0.62rem] tracking-[0.2em] text-white/55 uppercase">
        Transferred from the cabinet
      </p>
      <ul className="mt-3 grid gap-3 sm:grid-cols-2">
        {cases.map((c) => (
          <CaseRow key={c.case_id} c={c} />
        ))}
      </ul>
    </section>
  );
}

function RunReceipt({ result }: { result: RunResult }) {
  const steps = receiptSteps(result);
  if (steps.length === 0) return null;
  return (
    <ol
      aria-label="Run receipt"
      className="mt-6 space-y-1 border-t border-white/10 pt-4 font-mono text-[0.7rem] text-white/80"
    >
      {steps.map((step) => (
        <li key={step.seq} className="flex gap-3">
          <span className="tabular-nums text-white/45">{String(step.seq).padStart(2, "0")}</span>
          <span className="uppercase tracking-[0.12em]">{step.kind}</span>
          {step.detail && <span className="text-white/55">{step.detail}</span>}
        </li>
      ))}
    </ol>
  );
}

function Resolved({ runId, result }: { runId: string; result: RunResult }) {
  const outcome = outcomeOf(result);
  const committed = result.committed ?? [];

  const copy = {
    committed: {
      stamp: "Approved",
      tone: "var(--color-stamp)",
      line: `Committed for attorney review: ${committed.join(", ")}. Each carries a cover memo whose figures the system generated, never the model.`,
    },
    deferred: {
      stamp: "Deferred",
      tone: "#d9d4c8",
      line: "Nothing was committed. The cases stay on the queue, still owed a decision, which is a legitimate outcome and not a failure.",
    },
    unresolved: {
      stamp: "Unresolved",
      tone: "var(--color-flag)",
      line: "That answer was not accepted as a decision. Because no human actually resolved these cases, the deterministic floor committed them for LATER review and the run reports failure rather than success. An unattended system that guessed here would have reported green over an urgent case.",
    },
  }[outcome];

  return (
    <div>
      <Heading>Run {runId}</Heading>
      <div className="mt-3 flex flex-wrap items-center gap-5">
        <Stamp label={copy.stamp} tone={copy.tone} />
        <p className="max-w-2xl text-[0.95rem] leading-snug text-white/80">{copy.line}</p>
      </div>

      <dl className="mt-6 grid grid-cols-2 gap-x-8 gap-y-3 font-mono text-[0.66rem] sm:grid-cols-4">
        {[
          ["Attorney action", result.attorney_action || "none"],
          ["Committed", String(committed.length)],
          ["Run reports", result.succeeded ? "success" : "FAILURE"],
          ["Deterministic floor", result.backstop_used ? "delivered the sweep" : "not needed"],
        ].map(([label, value]) => (
          <div key={label}>
            <dt className="tracking-[0.18em] text-white/55 uppercase">{label}</dt>
            <dd className="mt-0.5 text-white/80">{value}</dd>
          </div>
        ))}
      </dl>

      {(result.failures?.length ?? 0) > 0 && (
        <p className="mt-4 font-mono text-[0.66rem] text-[var(--color-flag)]">
          Still owed a human decision: {result.failures?.join(", ")}
        </p>
      )}

      <RunReceipt result={result} />

      <button
        type="button"
        onClick={() => window.location.reload()}
        className="mt-7 font-mono text-[0.66rem] tracking-[0.12em] text-white/55 uppercase underline decoration-white/40 underline-offset-4 hover:text-white/80"
      >
        Run another sweep
      </button>
    </div>
  );
}
