import { useState } from "react";
import { DoorError } from "../data";

/**
 * Photograph a summons. Nova Pro transcribes printed fields. The engine
 * computes the deadline. The page never uses a model-invented date.
 */

type Extracted = {
  service_date: string;
  service_method: string;
  summons_stated_deadline: string | null;
  case_id: string;
};

type Ready = {
  extracted: Extracted;
  computed_deadline: string | null;
  effective_deadline: string | null;
  citation: string;
  flags: { code: string; reason: string }[];
  trace: { day: string; label: string }[];
  label: string;
};

type State = { k: "idle" } | { k: "working" } | { k: "ready"; result: Ready } | { k: "failed"; message: string };

async function postImage(file: Blob, mediaType: string): Promise<Ready> {
  const buf = await file.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  const image_b64 = btoa(binary);
  const response = await fetch("/api/ocr", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ image_b64, media_type: mediaType }),
  });
  const body = (await response.json()) as Ready & { error?: string; detail?: string };
  if (!response.ok) {
    throw new DoorError(body.detail || body.error || `HTTP ${response.status}`, response.status, body);
  }
  return body;
}

export function SummonsScan() {
  const [state, setState] = useState<State>({ k: "idle" });

  async function send(file: Blob, mediaType: string) {
    setState({ k: "working" });
    try {
      const result = await postImage(file, mediaType);
      setState({ k: "ready", result });
    } catch (e) {
      setState({
        k: "failed",
        message: e instanceof Error ? e.message : String(e),
      });
    }
  }

  async function sample() {
    try {
      const res = await fetch("/sample-summons.jpg");
      if (!res.ok) {
        setState({ k: "failed", message: "The sample summons is not on this door." });
        return;
      }
      await send(await res.blob(), "image/jpeg");
    } catch (e) {
      // A network-level throw (offline, blocked) must not strand the UI at
      // idle with no message.
      setState({ k: "failed", message: e instanceof Error ? e.message : String(e) });
    }
  }

  return (
    <section id="ocr" className="border-b border-white/10 bg-[var(--color-ground-soft)]">
      <div className="mx-auto max-w-[1400px] px-5 py-10 sm:px-10">
        <p className="font-mono text-[0.66rem] tracking-[0.2em] text-white/55 uppercase">
          Summons intake
        </p>
        <h2 className="display mt-2 text-[clamp(1.8rem,4vw,3rem)] leading-none">
          Photograph the page
        </h2>
        <p className="mt-3 max-w-2xl text-[0.95rem] leading-snug text-white/80">
          Nova Pro reads the printed service date. The engine computes the
          statutory last day. A date the model invented is a refusal. Sample
          summons is EXAMPLE DATA.
        </p>
        <div className="mt-6 flex flex-wrap items-center gap-3">
          <label className="font-mono text-[0.66rem] tracking-[0.16em] text-white uppercase">
            <input
              type="file"
              accept="image/png,image/jpeg,image/webp"
              className="block text-white/80"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) void send(file, file.type || "image/png");
              }}
            />
          </label>
          <button
            type="button"
            onClick={() => void sample()}
            className="rounded-[3px] border border-white/40 px-3 py-2 font-mono text-[0.66rem] tracking-[0.14em] text-white uppercase"
          >
            Try the sample summons
          </button>
        </div>
        {state.k === "working" && (
          <p className="mt-6 font-mono text-[0.7rem] text-white/55">Reading the page, then asking the engine.</p>
        )}
        {state.k === "failed" && (
          <p className="mt-6 max-w-2xl font-serif text-[1.05rem] text-white/80">
            No deadline is shown. {state.message}
          </p>
        )}
        {state.k === "ready" && (
          <div className="torn-edge paper-grain mt-8 max-w-3xl rounded-[3px] bg-[var(--color-paper)] p-6 text-[var(--color-ink)] shadow-[0_8px_0_rgba(0,0,0,0.25)]">
            <p className="font-mono text-[0.62rem] tracking-[0.18em] text-[var(--color-ink-soft)] uppercase">
              {state.result.label}
            </p>
            <dl className="mt-4 grid grid-cols-2 gap-x-8 gap-y-3 font-mono text-[0.78rem]">
              <div>
                <dt className="tracking-[0.16em] text-[var(--color-ink-soft)] uppercase">Printed service</dt>
                <dd className="mt-0.5 tabular-nums">{state.result.extracted.service_date}</dd>
              </div>
              <div>
                <dt className="tracking-[0.16em] text-[var(--color-ink-soft)] uppercase">Statutory last day</dt>
                <dd className="mt-0.5 tabular-nums" data-field="ocr-deadline">
                  {state.result.computed_deadline ?? "refused"}
                </dd>
              </div>
            </dl>
            <ol className="mt-4 space-y-1 font-mono text-[0.72rem] text-[var(--color-ink-soft)]">
              {state.result.trace.map((step, i) => (
                <li key={`${step.day}-${i}`}>
                  {step.day}: {step.label}
                </li>
              ))}
            </ol>
            <p className="mt-3 font-mono text-[0.62rem] text-[var(--color-ink-soft)]">
              {state.result.citation}
            </p>
          </div>
        )}
      </div>
    </section>
  );
}
