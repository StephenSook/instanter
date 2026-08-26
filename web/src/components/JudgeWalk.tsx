import { APK_URL, TESTFLIGHT_URL } from "../data";

/**
 * A numbered three-minute walk for a judge with no credentials.
 *
 * Every step is a deep link to a surface that already exists. This page
 * does not recompute anything; it points at the live door, the sweep, the
 * packet, TestFlight, the APK, and the raw JSON.
 */

const STEPS: { n: string; title: string; body: string; href: string; external?: boolean }[] = [
  {
    n: "01",
    title: "The number",
    body: "4 of 46 answer deadlines in this corpus are ones counting seven days by hand gets wrong. Recomputed on this request.",
    href: "/#live-proof",
  },
  {
    n: "02",
    title: "Sweep the queue",
    body: "A real AgentCore run. It stops at the attorney interrupt. You are the human.",
    href: "/#sweep",
  },
  {
    n: "03",
    title: "Send a typo",
    body: "After the sweep, tap 'or send an unclear answer'. The door refuses it as a decision and stamps FAILURE.",
    href: "/#sweep",
  },
  {
    n: "04",
    title: "Open a packet",
    body: "Case 26ED00101 is overdue. The packet is a court record: the trace is the engine's, the defence fields are blank on purpose.",
    href: "/#/case/26ED00101",
  },
  {
    n: "05",
    title: "What if they were served on",
    body: "Pick 8 Aug 2026. The paper shows Monday 17 Aug because Saturday rolls. The page does not invent the date.",
    href: "/#what-if",
  },
  {
    n: "06",
    title: "iOS, on a phone",
    body: "Public TestFlight. Apple approved Beta App Review on 2026-08-26.",
    href: TESTFLIGHT_URL,
    external: true,
  },
  {
    n: "07",
    title: "Android APK",
    body: "GitHub Release asset, signed with our key. Build-service links expire; this one does not.",
    href: APK_URL,
    external: true,
  },
  {
    n: "08",
    title: "Count the rows yourself",
    body: "The raw JSON for the headline, plus the two lists that add up to 4.",
    href: "/api/stats",
    external: true,
  },
  {
    n: "09",
    title: "Photograph a summons",
    body: "Nova Pro transcribes the printed service date. The engine computes the last day. A guessed date is a refusal.",
    href: "/#ocr",
  },
  {
    n: "10",
    title: "The statute on paper",
    body: "Every roll row, the summons-controlled date, and the citations. Linger here if you do not want to run a sweep.",
    href: "/evidence",
  },
];

export function JudgeWalk() {
  return (
    <section className="mx-auto max-w-3xl px-5 py-14 sm:px-10">
      <p className="font-mono text-[0.66rem] tracking-[0.2em] text-white/55 uppercase">
        Three minutes, no login
      </p>
      <h1 className="display mt-2 text-[clamp(2.4rem,8vw,5.5rem)] leading-none">
        Walk the door
      </h1>
      <p className="mt-4 max-w-2xl font-serif text-[1.1rem] leading-snug text-white/75">
        Each step is a live surface. Nothing on this list is a screenshot or a
        stored number. Start at 01 and stop when you have seen enough.
      </p>

      <ol className="mt-10 space-y-4">
        {STEPS.map((step) => (
          <li key={step.n}>
            <a
              href={step.href}
              {...(step.external
                ? { target: "_blank", rel: "noreferrer" }
                : {})}
              className="torn-edge paper-grain block rounded-[3px] bg-[var(--color-paper)] p-5 text-[var(--color-ink)] no-underline shadow-[0_6px_0_rgba(0,0,0,0.28)] transition-transform duration-200 hover:-translate-y-0.5"
            >
              <p className="font-mono text-[0.62rem] tracking-[0.2em] text-[var(--color-ink-soft)] uppercase">
                {step.n}
              </p>
              <h2 className="display mt-1 text-[1.7rem] leading-none">{step.title}</h2>
              <p className="mt-2 font-serif text-[1.02rem] leading-snug">{step.body}</p>
              <p className="mt-3 font-mono text-[0.62rem] tracking-[0.14em] text-[var(--color-ink-soft)] uppercase">
                {step.href}
              </p>
            </a>
          </li>
        ))}
      </ol>
    </section>
  );
}
