import { useEffect, useState } from "react";

/**
 * The line that says the agent runs itself.
 *
 * The product's claim is that a walk-in clinic cannot watch every clock. That
 * is only true if the sweep fires without anybody opening this page, and until
 * it did, the claim was the one thing the product did not do. EventBridge
 * Scheduler now runs it at 7am on weekdays in the court's own timezone.
 *
 * Three states, ONE structure. The previous layout-shift work established why:
 * a block that appears after its fetch resolves pushes everything below it, and
 * a reserved pixel height is right at exactly one viewport width. Rendering the
 * same single line in every state makes the states identical by construction.
 *
 * Nothing here is invented. If the door cannot be reached, this says so and
 * shows no count, because a decision surface that fabricates a number is worse
 * than one that admits it is blind.
 */

type Awaiting = {
  run_id: string;
  origin: string;
  created_at: number;
  cases: number;
};

type State =
  | { k: "loading" }
  | { k: "failed"; message: string }
  | { k: "loaded"; awaiting: Awaiting[] };

function when(seconds: number): string {
  if (!seconds) return "";
  const then = new Date(seconds * 1000);
  const mins = Math.round((Date.now() - then.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return then.toLocaleDateString();
}

export function SweepBanner() {
  const [state, setState] = useState<State>({ k: "loading" });

  useEffect(() => {
    let live = true;
    fetch("/api/awaiting", { headers: { accept: "application/json" } })
      .then(async (r) => {
        if (!r.ok) throw new Error(`the door returned ${r.status}`);
        return (await r.json()) as { awaiting: Awaiting[] };
      })
      .then((d) => live && setState({ k: "loaded", awaiting: d.awaiting ?? [] }))
      .catch((e: Error) => live && setState({ k: "failed", message: e.message }));
    return () => {
      live = false;
    };
  }, []);

  const scheduled =
    state.k === "loaded" ? state.awaiting.filter((a) => a.origin === "scheduled") : [];
  const cases = scheduled.reduce((n, a) => n + a.cases, 0);

  return (
    <div className="border-b border-white/10 bg-black/30">
      <div className="mx-auto flex max-w-[1400px] flex-wrap items-baseline gap-x-3 gap-y-1 px-5 py-3 sm:px-10">
        <span className="text-[0.7rem] tracking-[0.18em] text-white/40 uppercase">
          Scheduled sweep
        </span>
        <span className="text-[0.9rem] leading-snug text-white/70">
          {state.k === "loading" && "Checking what the sweep left."}
          {state.k === "failed" &&
            `Cannot reach the door (${state.message}), so no count is shown.`}
          {state.k === "loaded" && scheduled.length === 0 && (
            <>
              Runs on its own at 7am on weekdays, court time. Nothing is waiting on an
              attorney right now.
            </>
          )}
          {state.k === "loaded" && scheduled.length > 0 && (
            <>
              <strong className="text-[var(--color-flag)]">
                {cases} case{cases === 1 ? "" : "s"}
              </strong>{" "}
              waiting on an attorney, from a sweep that ran with nobody watching
              {scheduled[0].created_at ? ` ${when(scheduled[0].created_at)}` : ""}. Nothing
              in it is committed until someone answers.
            </>
          )}
        </span>
      </div>
    </div>
  );
}
