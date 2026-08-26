import { useEffect, useState } from "react";
import { loadStats, type Stats } from "../data";

/**
 * The 4 of 46, as lists a judge can count, with the statute named.
 *
 * Same fail-closed rule as LiveProof: if the door does not answer, no number
 * is shown. The page never copies the lists into itself.
 */

export function EvidencePage() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadStats()
      .then(setStats)
      .catch((e: Error) => setError(e.message));
  }, []);

  return (
    <section className="mx-auto max-w-3xl px-5 py-14 sm:px-10">
      <p className="font-mono text-[0.66rem] tracking-[0.2em] text-white/55 uppercase">
        Statute on paper
      </p>
      <h1 className="display mt-2 text-[clamp(2.4rem,8vw,5.5rem)] leading-none">
        Why seven days is not seven days
      </h1>

      {error && (
        <p className="mt-8 font-serif text-[1.1rem] text-white/80">
          Live recomputation unavailable ({error}). This page shows no number when
          it cannot reach the door.
        </p>
      )}

      {!stats && !error && (
        <p className="mt-8 font-mono text-[0.7rem] text-white/55">
          Recomputing every answer deadline in the corpus.
        </p>
      )}

      {stats && <EvidenceBody stats={stats} />}
    </section>
  );
}

function EvidenceBody({ stats }: { stats: Stats }) {
  const rolls = stats.because_the_deadline_rolls;
  const summons = stats.because_the_summons_controls;
  return (
    <>
      <p className="mt-6 font-serif text-[1.15rem] leading-snug text-white/80">
        <span className="display mr-2 text-[2.4rem] leading-none tabular-nums">
          {stats.headline.answer_deadlines_hand_counting_gets_wrong}
        </span>
        of {stats.headline.of_deadlines_computed} computed deadlines in this
        corpus are ones counting seven days by hand gets wrong.{" "}
        {stats.headline.why_it_matters}
      </p>
      <p className="mt-3 font-mono text-[0.62rem] text-white/55">
        Recomputed {stats.recomputed_at} in {stats.computation.elapsed_ms} ms ·{" "}
        <a href="/api/stats" className="underline decoration-white/40">
          raw JSON
        </a>
      </p>

      <div className="torn-edge paper-grain mt-10 rounded-[3px] bg-[var(--color-paper)] p-6 text-[var(--color-ink)] shadow-[0_8px_0_rgba(0,0,0,0.25)]">
        <h2 className="font-mono text-[0.62rem] tracking-[0.2em] text-[var(--color-ink-soft)] uppercase">
          Wrong because it rolls
        </h2>
        <p className="mt-2 font-serif text-[1.02rem] leading-snug">
          O.C.G.A. 1-3-1(d)(3). Day seven lands on a Saturday or Sunday, so the
          last day to answer is the next day the court is open.
        </p>
        <ul className="mt-4 space-y-3 font-mono text-[0.78rem]">
          {rolls.map((row) => (
            <li key={row.case_id} className="border-t border-[var(--color-paper-edge)] pt-3">
              <a href={`/#/case/${row.case_id}`} className="font-semibold underline">
                {row.case_id}
              </a>
              <p className="mt-1 text-[var(--color-ink-soft)]">
                served {row.served} · hand-counted {row.hand_counted} (
                {row.hand_counted_weekday}) · statutory {row.statutory} (
                {row.statutory_weekday}) · {row.days_off} day
                {row.days_off === 1 ? "" : "s"} off
              </p>
            </li>
          ))}
        </ul>
      </div>

      <div className="torn-edge paper-grain mt-6 rounded-[3px] bg-[var(--color-paper)] p-6 text-[var(--color-ink)] shadow-[0_8px_0_rgba(0,0,0,0.25)]">
        <h2 className="font-mono text-[0.62rem] tracking-[0.2em] text-[var(--color-ink-soft)] uppercase">
          Wrong because the summons controls
        </h2>
        <p className="mt-2 font-serif text-[1.02rem] leading-snug">
          O.C.G.A. 44-7-51(b). Where the summons states a date that differs from
          the computation, the stated date binds the tenant.
        </p>
        <ul className="mt-4 space-y-3 font-mono text-[0.78rem]">
          {summons.map((row) => (
            <li key={row.case_id} className="border-t border-[var(--color-paper-edge)] pt-3">
              <a href={`/#/case/${row.case_id}`} className="font-semibold underline">
                {row.case_id}
              </a>
              <p className="mt-1 text-[var(--color-ink-soft)]">
                computed {row.computed} · controlling{" "}
                <span data-field="summons-controlling">{row.controlling}</span> · {row.authority}
              </p>
            </li>
          ))}
        </ul>
      </div>

      <p className="mt-8 font-mono text-[0.66rem] leading-relaxed text-white/55">
        {stats.computation.citation}. Legal holidays: O.C.G.A. 1-4-1. A courthouse
        closed on a day that is not a legal holiday does not move the statute.
        That is the December 31 trap on the paper calendar.
      </p>
    </>
  );
}
