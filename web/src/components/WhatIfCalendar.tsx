import { useState } from "react";
import { DoorError, loadWhatIf, type WhatIf } from "../data";

/**
 * A paper calendar that displays ONE statutory computation.
 *
 * The seven-day count, any weekend/holiday roll, and the December 31 trap
 * come from `/api/what-if`, which calls the same `compute_deadline` the
 * Python tests cover. This component never adds a day, never rolls a
 * terminal date, and never invents a flag. If the door does not answer,
 * it says so and shows no deadline.
 */

type State =
  | { k: "idle" }
  | { k: "loading"; date: string }
  | { k: "ready"; date: string; result: WhatIf }
  | { k: "failed"; date: string; message: string };

const WEEKEND = "2026-08-08";
const DEC31 = "2026-12-24";

export function WhatIfCalendar() {
  const [picked, setPicked] = useState("");
  const [state, setState] = useState<State>({ k: "idle" });

  async function ask(date: string) {
    if (!date) return;
    setPicked(date);
    setState({ k: "loading", date });
    try {
      const result = await loadWhatIf(date);
      setState({ k: "ready", date, result });
    } catch (e) {
      const message = e instanceof DoorError || e instanceof Error ? e.message : String(e);
      setState({ k: "failed", date, message });
    }
  }

  const closed = state.k === "ready" &&
    state.result.flags.some((f) => f.code === "court_closed_not_legal_holiday");

  return (
    <section className="border-b border-white/10 bg-[var(--color-ground-soft)]">
      <div className="mx-auto max-w-[1400px] px-5 py-10 sm:px-10">
        <p className="font-mono text-[0.66rem] tracking-[0.2em] text-white/55 uppercase">
          Paper calendar
        </p>
        <h2 className="display mt-2 text-[clamp(1.8rem,4vw,3rem)] leading-none">
          What if they were served on
        </h2>
        <p className="mt-3 max-w-2xl text-[0.95rem] leading-snug text-white/80">
          Pick a service date. The engine counts seven days from the statute, rolls a
          terminal Saturday or legal holiday, and flags December 31 2026 when the
          courthouse is closed but the statute does not move. Nothing on this paper is
          a date the page invented.
        </p>

        <div className="mt-6 flex flex-wrap items-end gap-3">
          <label className="font-mono text-[0.66rem] tracking-[0.18em] text-white/55 uppercase">
            Service date
            <input
              type="date"
              value={picked}
              onChange={(e) => void ask(e.target.value)}
              className="mt-1 block rounded-[3px] border border-[var(--color-paper-edge)] bg-[var(--color-paper)] px-3 py-2 font-mono text-[0.9rem] text-[var(--color-ink)]"
            />
          </label>
          <button
            type="button"
            onClick={() => void ask(WEEKEND)}
            className="rounded-[3px] border border-white/40 px-3 py-2 font-mono text-[0.66rem] tracking-[0.14em] text-white uppercase"
          >
            Weekend roll, 8 Aug 2026
          </button>
          <button
            type="button"
            onClick={() => void ask(DEC31)}
            className="rounded-[3px] border border-white/40 px-3 py-2 font-mono text-[0.66rem] tracking-[0.14em] text-white uppercase"
          >
            Dec 31 trap, served 24 Dec
          </button>
        </div>

        {state.k === "loading" && (
          <p className="mt-6 font-mono text-[0.7rem] text-white/55">Asking the engine.</p>
        )}

        {state.k === "failed" && (
          <p className="mt-6 max-w-2xl font-serif text-[1.05rem] text-white/80">
            The engine did not answer. No deadline is shown. {state.message}
          </p>
        )}

        {state.k === "ready" && (
          <div className="paper-grain mt-8 max-w-3xl rounded-[3px] bg-[var(--color-paper)] p-6 text-[var(--color-ink)] shadow-[0_8px_0_rgba(0,0,0,0.25)]">
            <p className="font-mono text-[0.62rem] tracking-[0.18em] text-[var(--color-ink-soft)] uppercase">
              {state.result.label}
            </p>
            <dl className="mt-4 grid grid-cols-2 gap-x-8 gap-y-3 font-mono text-[0.78rem]">
              <div>
                <dt className="tracking-[0.16em] text-[var(--color-ink-soft)] uppercase">
                  Served
                </dt>
                <dd className="mt-0.5 tabular-nums">{state.result.service_date}</dd>
              </div>
              <div>
                <dt className="tracking-[0.16em] text-[var(--color-ink-soft)] uppercase">
                  Statutory last day
                </dt>
                <dd className="mt-0.5 tabular-nums" data-field="statutory-deadline">
                  {state.result.computed_deadline ?? "refused"}
                </dd>
              </div>
            </dl>

            {closed && (
              <p
                className="mt-5 inline-block -rotate-3 border-[3px] border-[var(--color-stamp)] px-3 py-1 font-mono text-[0.95rem] font-bold tracking-[0.18em] text-[var(--color-stamp)] uppercase"
                data-flag="court_closed_not_legal_holiday"
              >
                Court closed
              </p>
            )}

            <ol className="mt-5 space-y-1.5 font-mono text-[0.7rem] leading-snug">
              {state.result.trace.map((step) => (
                <li key={`${step.day}-${step.label}`}>
                  <span className="tabular-nums">{step.day}</span>
                  <span className="text-[var(--color-ink-soft)]">: {step.label}</span>
                </li>
              ))}
            </ol>
            <p className="mt-4 font-mono text-[0.62rem] text-[var(--color-ink-soft)]">
              {state.result.citation}
            </p>
          </div>
        )}
      </div>
    </section>
  );
}
