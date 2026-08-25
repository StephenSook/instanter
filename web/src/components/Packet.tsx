import { useEffect, useRef } from "react";
import gsap from "gsap";
import { BANDS, countdown, formatFlag, type Case } from "../data";

/** One case as an OPEN FOLDER: the band colour floods the field, a cream
 *  document sits inside it, and the neighbouring cases stay reachable as
 *  physical tabs down the right edge.
 *
 *  The document is a court record, not a dashboard card. Everything
 *  printed on it is engine output: the statutory computation is shown day
 *  by day, the flags carry the engine's own reason texts, and the draft
 *  answer's defence fields are VISIBLY blank, which is the UPL boundary
 *  made into something a reader can see. */
export function Packet({
  caseRecord,
  neighbours,
  onOpen,
  onBack,
}: {
  caseRecord: Case;
  neighbours: Case[];
  onOpen: (id: string) => void;
  onBack: () => void;
}) {
  const band = BANDS[caseRecord.level];
  const rootRef = useRef<HTMLDivElement>(null);
  const overdue = caseRecord.days_remaining !== null && caseRecord.days_remaining < 0;

  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) return;
    const ctx = gsap.context(() => {
      gsap.from("[data-sheet]", { yPercent: 3, opacity: 0, duration: 0.6, ease: "power3.out" });
      gsap.from("[data-stamp]", {
        scale: 1.7,
        opacity: 0,
        rotate: -18,
        duration: 0.45,
        delay: 0.35,
        ease: "back.out(2.2)",
      });
      gsap.from("[data-trace-row]", {
        opacity: 0,
        x: -10,
        duration: 0.32,
        stagger: 0.07,
        delay: 0.45,
        ease: "power2.out",
      });
    }, rootRef);
    return () => ctx.revert();
  }, [caseRecord.case_id]);

  return (
    <div ref={rootRef}>
      <div className="mx-auto max-w-[1400px] px-5 pt-10 sm:px-10 sm:pt-16">
        <button
          type="button"
          onClick={onBack}
          className="font-mono text-[0.68rem] tracking-[0.18em] text-white/50 uppercase transition-colors hover:text-white"
        >
          &larr; Back to the cabinet
        </button>
        <h1 className="display mt-5 text-[clamp(2.6rem,8vw,6.5rem)]">{caseRecord.case_id}</h1>
        <p className="mt-3 font-mono text-[0.72rem] tracking-[0.16em] text-white/55 uppercase">
          {band.label} &middot; rank {caseRecord.rank} &middot; {countdown(caseRecord.days_remaining)}
        </p>
      </div>

      {/* The open folder. */}
      <div className="relative mt-8" style={{ background: band.color }}>
        <div className="mx-auto flex max-w-[1400px] gap-0 px-5 py-8 sm:px-10 sm:py-12">
          <article
            data-sheet
            className="paper-grain relative w-full rounded-[2px] bg-[var(--color-paper)] px-5 py-8 text-[var(--color-ink)] shadow-[0_10px_40px_rgba(0,0,0,0.35)] sm:px-10 sm:py-12"
          >
            {/* Punch holes, coloured to the folder. */}
            <span aria-hidden="true" className="absolute top-0 bottom-0 left-3 hidden w-3 sm:block">
              {[18, 48, 78].map((top) => (
                <span
                  key={top}
                  className="absolute h-3 w-3 rounded-full"
                  style={{ top: `${top}%`, background: band.color }}
                />
              ))}
            </span>

            {overdue && (
              <span
                data-stamp
                className="absolute top-6 right-5 rotate-[-11deg] rounded-[3px] border-[3px] px-3 py-1 font-mono text-[0.8rem] font-semibold tracking-[0.14em] uppercase sm:right-12"
                style={{ color: "var(--color-stamp)", borderColor: "var(--color-stamp)" }}
              >
                Deadline passed
              </span>
            )}

            <div className="grid gap-8 sm:ml-8 sm:grid-cols-[minmax(0,15rem)_1fr] sm:gap-12">
              {/* Left column: the record card, mono, like a case jacket. */}
              <div>
                <div className="relative">
                  <span
                    aria-hidden="true"
                    className="absolute -top-3 left-1/2 h-8 w-4 -translate-x-1/2 rounded-full border-2 border-[var(--color-ink-soft)]/60"
                  />
                  <div className="border border-[var(--color-paper-edge)] bg-white/60 p-4">
                    <p className="font-mono text-[0.6rem] tracking-[0.18em] text-[var(--color-ink-soft)] uppercase">
                      Served
                    </p>
                    <p className="font-mono text-[0.95rem] font-semibold">{caseRecord.service_date ?? "unknown"}</p>
                    <p className="mt-3 font-mono text-[0.6rem] tracking-[0.18em] text-[var(--color-ink-soft)] uppercase">
                      Method
                    </p>
                    <p className="font-mono text-[0.95rem]">{caseRecord.service_method ?? "unknown"}</p>
                  </div>
                </div>

                <dl className="mt-6 space-y-3 font-mono text-[0.78rem]">
                  <Field label="Answer due" value={caseRecord.effective_deadline ?? "not established"} strong />
                  <Field label="Computed" value={caseRecord.computed_deadline ?? "refused"} />
                  <Field label="Basis" value={caseRecord.deadline_basis.replace(/_/g, " ")} />
                  {caseRecord.court_reopens_on && (
                    <Field label="Clerk reopens" value={caseRecord.court_reopens_on} />
                  )}
                  <Field label="Answer on docket" value={caseRecord.answer_filed ? "yes" : "no"} />
                </dl>

                <p className="mt-6 border-t border-[var(--color-paper-edge)] pt-3 font-mono text-[0.62rem] leading-relaxed text-[var(--color-ink-soft)]">
                  {caseRecord.citation || "no citation on a refused computation"}
                </p>
                <p className="mt-4 inline-block bg-[var(--color-flag)]/70 px-1.5 py-0.5 font-mono text-[0.58rem] tracking-wide uppercase">
                  {caseRecord.label}
                </p>
              </div>

              {/* Right column: the reasoning, in serif, with a drop cap. */}
              <div>
                <h2 className="font-mono text-[0.62rem] tracking-[0.2em] text-[var(--color-ink-soft)] uppercase">
                  Why this case, and not the others
                </h2>
                <Rationale caseRecord={caseRecord} />

                <h3 className="mt-8 font-mono text-[0.62rem] tracking-[0.2em] text-[var(--color-ink-soft)] uppercase">
                  How the deadline was computed
                </h3>
                <ol className="mt-3 border-l-2 border-[var(--color-paper-edge)] pl-4">
                  {caseRecord.trace.map((step, i) => (
                    <li key={`${step.day}-${i}`} data-trace-row className="relative py-1.5">
                      <span
                        aria-hidden="true"
                        className="absolute top-3 -left-[1.32rem] h-1.5 w-1.5 rounded-full"
                        style={{ background: band.color }}
                      />
                      <span className="font-mono text-[0.74rem] font-semibold">{step.day}</span>
                      <span className="ml-2 font-serif text-[0.95rem] text-[var(--color-ink)]/85">{step.label}</span>
                    </li>
                  ))}
                </ol>

                {caseRecord.flags.length > 0 && (
                  <>
                    <h3 className="mt-8 font-mono text-[0.62rem] tracking-[0.2em] text-[var(--color-ink-soft)] uppercase">
                      Staff must confirm
                    </h3>
                    <ul className="mt-3 space-y-2">
                      {caseRecord.flags.map((f) => (
                        <li key={f.code} className="bg-[var(--color-flag)]/35 p-2.5">
                          <p className="font-mono text-[0.6rem] font-semibold tracking-wide uppercase">
                            {formatFlag(f.code)}
                            {f.day ? ` · ${f.day}` : ""}
                          </p>
                          <p className="mt-1 font-serif text-[0.92rem] leading-snug">{f.reason}</p>
                        </li>
                      ))}
                    </ul>
                  </>
                )}

                {/* The UPL boundary, drawn. */}
                <div className="mt-8 border border-dashed border-[var(--color-ink-soft)]/45 p-4">
                  <h3 className="font-mono text-[0.62rem] tracking-[0.2em] text-[var(--color-ink-soft)] uppercase">
                    Draft answer skeleton
                  </h3>
                  <p className="mt-2 font-serif text-[0.9rem] text-[var(--color-ink)]/75">
                    Caption and dates are filled from the record above. Every defence field is left blank
                    on purpose: selecting a defence is legal judgement, and only the reviewing attorney
                    makes it.
                  </p>
                  <ul className="mt-3 space-y-2 font-mono text-[0.74rem]">
                    {["Defence 1", "Defence 2", "Counterclaim"].map((slot) => (
                      <li key={slot} className="flex items-center gap-3">
                        <span className="h-3.5 w-3.5 border border-[var(--color-ink-soft)]" aria-hidden="true" />
                        <span className="text-[var(--color-ink-soft)]">{slot}</span>
                        <span className="h-px flex-1 bg-[var(--color-paper-edge)]" aria-hidden="true" />
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            </div>
          </article>

          {/* Tab rail: neighbouring cases stay physically reachable. */}
          <nav aria-label="Nearby cases" className="ml-1 hidden w-11 shrink-0 flex-col gap-1 lg:flex">
            {neighbours.map((n) => (
              <button
                key={n.case_id}
                type="button"
                onClick={() => onOpen(n.case_id)}
                style={{ background: BANDS[n.level].color }}
                className="rail-tab flex-1 rounded-r-[10px] py-4 font-serif text-[0.8rem] text-white transition-transform duration-200 ease-[var(--ease-folder)] hover:translate-x-1"
              >
                {n.case_id}
              </button>
            ))}
          </nav>
        </div>
      </div>
    </div>
  );
}

/** The rationale is deliberately two things joined: deterministic facts the
 *  engine rendered, then the model's own digitless explanation. The packet
 *  already prints those facts as fields, so here we show the WORDS and say
 *  whose they are. Separating them is the product's central claim made
 *  visible: the machine states figures, the model only explains. */
function Rationale({ caseRecord }: { caseRecord: Case }) {
  const raw = caseRecord.rationale ?? "";
  const marker = "Writer explanation:";
  const templated = raw.includes("[MODEL DISABLED");
  const prose = raw.includes(marker) ? raw.slice(raw.indexOf(marker) + marker.length).trim() : "";

  if (prose) {
    return (
      <>
        <p className="mt-3 font-serif text-[1.05rem] leading-[1.6] first-letter:float-left first-letter:mt-1 first-letter:mr-2 first-letter:text-[3.6rem] first-letter:leading-[0.78]">
          {prose}
        </p>
        <p className="mt-2 font-mono text-[0.58rem] tracking-[0.14em] text-[var(--color-ink-soft)] uppercase">
          Written by the escalation model &middot; every figure on this sheet comes from the engine
        </p>
      </>
    );
  }

  // No model prose: show the ladder's own discriminators rather than
  // inventing a narrative, and label the template honestly.
  return (
    <>
      <ul className="mt-3 space-y-1.5">
        {caseRecord.factors.map((factor) => (
          <li key={factor} className="font-serif text-[1rem] leading-snug">
            {factor}
          </li>
        ))}
      </ul>
      <p className="mt-2 font-mono text-[0.58rem] tracking-[0.14em] text-[var(--color-ink-soft)] uppercase">
        {templated
          ? "Deterministic factors · the model layer was disabled for this case"
          : "Deterministic factors from the triage ladder"}
      </p>
    </>
  );
}

function Field({ label, value, strong }: { label: string; value: string; strong?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-[var(--color-paper-edge)] pb-1.5">
      <dt className="text-[0.6rem] tracking-[0.16em] text-[var(--color-ink-soft)] uppercase">{label}</dt>
      <dd className={strong ? "font-semibold" : ""}>{value}</dd>
    </div>
  );
}
