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
 *  Every state renders the same STRUCTURE, so the panel occupies identical
 *  space before and after its fetch resolves. See Skeleton below for why a
 *  reserved height was the wrong tool for that.
 */

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <section className="border-b border-white/10 bg-black/30">
      <div className="mx-auto max-w-[1400px] px-5 py-6 sm:px-10">
        {children}
      </div>
    </section>
  );
}

/** The loading state renders the SAME structure as the loaded one, with the
 *  figures replaced by placeholders.
 *
 *  A reserved min-height was tried first and was the wrong tool: the panel's
 *  real height depends on how the prose wraps, so a fixed reservation is a
 *  guess that is only correct at one viewport. It was 63px short at 1350px
 *  wide, and the resulting jump measured 0.53 of cumulative layout shift on
 *  its own, which shoved the whole queue down the page as the number arrived.
 *  Matching the structure makes the two states the same height by
 *  construction, at every width, with no number to maintain.
 */
function Skeleton({ note }: { note: string }) {
  const dash = "–";
  return (
    <>
      <div className="flex flex-wrap items-baseline gap-x-5 gap-y-2">
        <span className="display text-[2.6rem] leading-none text-white/20 tabular-nums">
          {dash}
        </span>
        <p className="max-w-2xl text-[0.95rem] leading-snug text-white/55">
          {note}
        </p>
      </div>
      <dl className="mt-5 grid grid-cols-2 gap-x-8 gap-y-2.5 font-mono text-[0.66rem] text-white/55 sm:grid-cols-4">
        {[
          "Wrong because it rolls",
          "Wrong because the summons controls",
          "Recomputed just now, in",
          "Refused as unverified",
        ].map((label) => (
          <div key={label}>
            <dt className="tracking-[0.18em] text-white/55 uppercase">
              {label}
            </dt>
            <dd className="mt-0.5 text-white/20 tabular-nums">{dash}</dd>
          </div>
        ))}
      </dl>
      <p className="mt-4 font-mono text-[0.62rem] leading-relaxed text-white/55">
        O.C.G.A. 44-7-51(b); O.C.G.A. 1-3-1(d)(3); O.C.G.A. 1-4-1
      </p>
    </>
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
        <Skeleton
          note={`Live recomputation unavailable (${error}). This panel calls the deployed door, and shows no number when it cannot reach one.`}
        />
      </Shell>
    );
  }

  if (!stats) {
    return (
      <Shell>
        <Skeleton note="Recomputing every answer deadline in the corpus." />
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
          of {headline.of_deadlines_computed} answer deadlines in this corpus
          are ones{" "}
          <em className="text-white not-italic">
            counting seven days by hand gets wrong.
          </em>{" "}
          {headline.why_it_matters}
        </p>
      </div>

      <dl className="mt-5 grid grid-cols-2 gap-x-8 gap-y-2.5 font-mono text-[0.66rem] text-white/55 sm:grid-cols-4">
        <div>
          <dt className="tracking-[0.18em] text-white/55 uppercase">
            Wrong because it rolls
          </dt>
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
          <dt className="tracking-[0.18em] text-white/55 uppercase">
            Recomputed just now, in
          </dt>
          <dd className="mt-0.5 text-white/70 tabular-nums">
            {computation.elapsed_ms} ms
          </dd>
        </div>
        <div>
          <dt className="tracking-[0.18em] text-white/55 uppercase">
            Refused as unverified
          </dt>
          <dd className="mt-0.5 text-white/70 tabular-nums">
            {computation.refused_unverified} of {corpus.cases}
          </dd>
        </div>
      </dl>

      <p className="mt-4 font-mono text-[0.62rem] leading-relaxed text-white/55">
        {computation.citation} &middot; recomputed at {stats.recomputed_at}{" "}
        &middot;{" "}
        <a
          href="/api/stats"
          className="underline decoration-white/50 hover:text-white/80"
        >
          read the raw JSON and count the rows yourself
        </a>
      </p>
    </Shell>
  );
}
