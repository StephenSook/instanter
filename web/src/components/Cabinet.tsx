import { useEffect, useRef, useState } from "react";
import gsap from "gsap";
import { BAND_ORDER, BANDS, countdown, formatFlag, type Case, type Level, type QueueSnapshot } from "../data";

const BAND_COPY: Record<Level, string> = {
  interrupt:
    "The statute has run out or runs out tomorrow. These go in front of a licensed attorney now, and only attorney capacity decides how many.",
  surface_today:
    "Close to the deadline, or the clock itself is in doubt. A human must look at these today, before they become interrupts.",
  monitor:
    "Computed, clear, and not yet urgent. The sweep will re-rank them tomorrow against the same statute.",
  hold: "An answer is already on the docket, so no default-writ exposure remains on this entry.",
};

/** The queue as a filing cabinet. One full-bleed band per urgency level,
 *  case tabs seated on the band's top edge. The band a case sits in is the
 *  ladder's own disposition, so the cabinet is a picture of the triage
 *  decision rather than decoration laid over one. */
export function Cabinet({ snapshot, onOpen }: { snapshot: QueueSnapshot; onOpen: (id: string) => void }) {
  const heroRef = useRef<HTMLDivElement>(null);
  const stackRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState<Level>("interrupt");

  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) return;
    const ctx = gsap.context(() => {
      gsap.from("[data-hero-line]", {
        yPercent: 110,
        duration: 0.9,
        ease: "power4.out",
        stagger: 0.08,
      });
      gsap.from("[data-band]", {
        yPercent: 26,
        opacity: 0,
        duration: 0.55,
        ease: "power4.out",
        stagger: 0.07,
        delay: 0.25,
      });
    });
    return () => ctx.revert();
  }, []);

  const groups = BAND_ORDER.map((band) => ({
    band,
    cases: snapshot.cases.filter((c) => c.level === band).sort((a, b) => a.rank - b.rank),
  })).filter((g) => g.cases.length > 0);

  return (
    <div className="pb-28">
      <div ref={heroRef} className="mx-auto max-w-[1400px] px-5 pt-14 pb-10 sm:px-10 sm:pt-24">
        <div className="overflow-hidden">
          <h1 data-hero-line className="display text-[clamp(2.9rem,9.5vw,8.5rem)]">
            The morning queue
          </h1>
        </div>
        <div className="mt-6 max-w-3xl">
          <p className="font-serif text-[clamp(1rem,1.6vw,1.3rem)] leading-relaxed text-white/65">
            Fulton County dispossessory intake for {snapshot.run_date}. Every answer deadline below was
            computed from the statute, never estimated by a model. The ladder ranks, attorney capacity
            rations, and a licensed human decides.
          </p>
        </div>
        <dl className="mt-10 grid max-w-3xl grid-cols-2 gap-x-10 gap-y-6 sm:grid-cols-4">
          <Stat label="Cases swept" value={snapshot.counts.total} />
          <Stat label="Interrupts" value={snapshot.counts.interrupt} tone="var(--color-l1)" />
          <Stat label="Flagged" value={snapshot.counts.flagged} tone="var(--color-flag)" />
          <Stat label="Audit events" value={snapshot.counts.audit_events} />
        </dl>
      </div>

      <div ref={stackRef} className="mt-4">
        {groups.map(({ band, cases }, index) => (
          <Band
            key={band}
            band={band}
            cases={cases}
            index={index}
            isOpen={open === band}
            onToggle={() => setOpen(band)}
            onOpen={onOpen}
          />
        ))}
      </div>
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div>
      <dt className="font-mono text-[0.62rem] tracking-[0.2em] text-white/40 uppercase">{label}</dt>
      <dd className="display mt-1.5 text-[2.6rem]" style={tone ? { color: tone } : undefined}>
        {value}
      </dd>
    </div>
  );
}

function Band({
  band,
  cases,
  index,
  isOpen,
  onToggle,
  onOpen,
}: {
  band: Level;
  cases: Case[];
  index: number;
  isOpen: boolean;
  onToggle: () => void;
  onOpen: (id: string) => void;
}) {
  const meta = BANDS[band];
  return (
    <section data-band className="relative" style={{ zIndex: 10 - index, marginTop: index === 0 ? 0 : "-0.4rem" }}>
      {/* Tabs seated on the band's top edge, tucking under the band above. */}
      <div className="mx-auto flex max-w-[1400px] flex-wrap items-end gap-1 px-5 sm:px-10">
        {cases.slice(0, 6).map((c, i) => (
          <button
            key={c.case_id}
            type="button"
            onClick={() => onOpen(c.case_id)}
            style={{ background: meta.color, marginLeft: i === 0 ? 0 : "-0.25rem", zIndex: 20 - i }}
            className="group relative rounded-t-[14px] px-5 pt-2.5 pb-3 text-left transition-transform duration-200 ease-[var(--ease-folder)] hover:-translate-y-1"
          >
            <span className="block font-serif text-[1.02rem] leading-none text-white">{c.case_id}</span>
            <span className="mt-1 block font-mono text-[0.6rem] tracking-wide text-white/75 uppercase">
              {countdown(c.days_remaining)}
            </span>
          </button>
        ))}
        {cases.length > 6 && (
          <span className="mb-2 ml-2 font-mono text-[0.62rem] text-white/40">
            +{cases.length - 6} more
          </span>
        )}
      </div>

      {/* The band body. */}
      <div style={{ background: meta.color }} className="w-full">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={isOpen}
          className="mx-auto flex w-full max-w-[1400px] items-center justify-between gap-6 px-5 py-4 text-left sm:px-10"
        >
          <span className="display text-[clamp(1.35rem,3.2vw,2.4rem)] text-white">{meta.label}</span>
          <span className="flex items-center gap-3 font-mono text-[0.66rem] tracking-[0.18em] text-white/85 uppercase">
            {meta.short} &middot; {cases.length} case{cases.length === 1 ? "" : "s"}
            <span aria-hidden="true" className="text-base leading-none">
              {isOpen ? "⌄" : "‹"}
            </span>
          </span>
        </button>

        {isOpen && (
          <div className="mx-auto max-w-[1400px] px-5 pb-8 sm:px-10">
            <p className="max-w-2xl font-mono text-[0.78rem] leading-relaxed text-white/90">
              {BAND_COPY[band]}
            </p>
            <ul className="mt-6 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {cases.map((c) => (
                <li key={c.case_id}>
                  <CaseCard caseRecord={c} onOpen={onOpen} />
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </section>
  );
}

function CaseCard({ caseRecord, onOpen }: { caseRecord: Case; onOpen: (id: string) => void }) {
  const overdue = caseRecord.days_remaining !== null && caseRecord.days_remaining < 0;
  const band = BANDS[caseRecord.level];

  return (
    <button
      type="button"
      onClick={() => onOpen(caseRecord.case_id)}
      className="paper-grain group block w-full rounded-[3px] bg-[var(--color-paper)] p-3.5 text-left text-[var(--color-ink)] shadow-[0_2px_0_rgba(0,0,0,0.28)] transition-transform duration-200 ease-[var(--ease-folder)] hover:-translate-y-0.5"
    >
      <span className="flex items-baseline justify-between gap-3">
        <span className="font-mono text-[0.92rem] font-semibold">{caseRecord.case_id}</span>
        <span
          className="font-mono text-[0.64rem] font-semibold tracking-wide uppercase"
          style={{ color: overdue ? "var(--color-stamp)" : "var(--color-ink-soft)" }}
        >
          {countdown(caseRecord.days_remaining)}
        </span>
      </span>
      <span className="mt-1.5 block font-serif text-[0.95rem] leading-snug">
        {caseRecord.effective_deadline
          ? `Answer due ${caseRecord.effective_deadline}`
          : "No reliable deadline established"}
      </span>
      <span className="mt-2 flex flex-wrap items-center gap-1">
        <span
          className="rounded-[2px] px-1.5 py-0.5 font-mono text-[0.58rem] tracking-wide text-white uppercase"
          style={{ background: band.color }}
        >
          {band.short}
        </span>
        {caseRecord.flags.slice(0, 2).map((f) => (
          <span
            key={f.code}
            className="rounded-[2px] bg-[var(--color-flag)] px-1.5 py-0.5 font-mono text-[0.58rem] tracking-wide text-[var(--color-ink)] uppercase"
          >
            {formatFlag(f.code)}
          </span>
        ))}
      </span>
      {caseRecord.held_reason && (
        <span className="mt-2 block font-mono text-[0.62rem] leading-snug text-[var(--color-ink-soft)]">
          {caseRecord.held_reason}
        </span>
      )}
    </button>
  );
}
