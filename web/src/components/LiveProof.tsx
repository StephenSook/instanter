import { useEffect, useState } from "react";
import { loadStats, type Stats } from "../data";

/** The one number a stranger can check without a key.
 *
 *  This strip does not display a figure written into the page. It calls the
 *  door, which recomputes every answer deadline in the corpus with the same
 *  engine the test suite covers, and reports what it found along with how long
 *  it took. The endpoint is linked so anyone can read the raw JSON and count
 *  the rows themselves.
 *
 *  When the door is unreachable (running the console locally, say) it says so
 *  plainly. A live-proof panel that falls back to a hardcoded number would be
 *  worse than no panel at all.
 *
 *  Every state renders inside the same fixed-height shell. The panel resolves
 *  after a network round trip, so a shell that grew when the numbers arrived
 *  would shove the whole queue down the page: measured at 0.22 cumulative
 *  layout shift before this was reserved.
 */

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <section className="border-b border-white/10 bg-black/30">
      <div className="mx-auto flex min-h-[13.5rem] max-w-[1400px] flex-col justify-center px-5 py-6 sm:min-h-[11.5rem] sm:px-10">
        {children}
      </div>
    </section>
  );
}

export function LiveProof() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadStats()
      .then(setStats)
      .catch((e: Error) => setError(e.message));
  }, []);

  if (error) {
    return (
      <Shell>
        <p className="font-mono text-[0.66rem] tracking-[0.18em] text-white/55 uppercase">
          Live recomputation unavailable ({error})
        </p>
        <p className="mt-1.5 font-mono text-[0.66rem] text-white/55">
          This panel calls the deployed door. It shows no number when it cannot reach one.
        </p>
      </Shell>
    );
  }

  if (!stats) {
    return (
      <Shell>
        <p className="font-mono text-[0.66rem] tracking-[0.18em] text-white/55 uppercase">
          Recomputing every deadline
        </p>
      </Shell>
    );
  }

  const { headline, computation, corpus } = stats;

  return (
    <Shell>
      <div className="flex flex-wrap items-baseline gap-x-5 gap-y-2">
        <span className="display text-[2.6rem] leading-none text-[var(--color-urgent,#d9483b)] tabular-nums">
          {headline.answer_deadlines_hand_counting_gets_wrong}
        </span>
        <p className="max-w-2xl text-[0.95rem] leading-snug text-white/80">
          of {headline.of_deadlines_computed} answer deadlines in this corpus are ones{" "}
          <em className="text-white not-italic">counting seven days by hand gets wrong.</em>{" "}
          {headline.why_it_matters}
        </p>
      </div>

      <dl className="mt-5 grid grid-cols-2 gap-x-8 gap-y-2.5 font-mono text-[0.66rem] text-white/55 sm:grid-cols-4">
        <div>
          <dt className="tracking-[0.18em] text-white/55 uppercase">Wrong because it rolls</dt>
          <dd className="mt-0.5 text-white/70 tabular-nums">
            {stats.because_the_deadline_rolls.length} land on a weekend
          </dd>
        </div>
        <div>
          <dt className="tracking-[0.18em] text-white/55 uppercase">
            Wrong because the summons controls
          </dt>
          <dd className="mt-0.5 text-white/70 tabular-nums">
            {stats.because_the_summons_controls.length} state a different date
          </dd>
        </div>
        <div>
          <dt className="tracking-[0.18em] text-white/55 uppercase">Recomputed just now, in</dt>
          <dd className="mt-0.5 text-white/70 tabular-nums">{computation.elapsed_ms} ms</dd>
        </div>
        <div>
          <dt className="tracking-[0.18em] text-white/55 uppercase">Refused as unverified</dt>
          <dd className="mt-0.5 text-white/70 tabular-nums">
            {computation.refused_unverified} of {corpus.cases}
          </dd>
        </div>
      </dl>

      <p className="mt-4 font-mono text-[0.62rem] leading-relaxed text-white/55">
        {computation.citation} &middot; recomputed at {stats.recomputed_at} &middot;{" "}
        <a href="/api/stats" className="underline decoration-white/50 hover:text-white/80">
          read the raw JSON and count the rows yourself
        </a>
      </p>
    </Shell>
  );
}
