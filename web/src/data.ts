/** The console renders engine output. Every field here is produced by
 *  `scripts/export_queue.py` from a real deterministic sweep; nothing in
 *  the UI invents a date, a day count, or a rank. */

export type Level = "interrupt" | "surface_today" | "monitor" | "hold";

export interface Flag {
  code: string;
  reason: string;
  day: string | null;
}

export interface TraceStep {
  day: string;
  label: string;
}

export interface Case {
  case_id: string;
  level: Level;
  floor_level: Level;
  rank: number;
  days_remaining: number | null;
  interrupt_now: boolean;
  held_reason: string | null;
  raised_by: string[];
  factors: string[];
  flags: Flag[];
  effective_deadline: string | null;
  computed_deadline: string | null;
  deadline_basis: string;
  citation: string;
  court_reopens_on: string | null;
  trace: TraceStep[];
  service_date: string | null;
  service_method: string | null;
  answer_filed: boolean;
  tenant_display_name: string;
  property_address: string;
  notes: string;
  label: string;
  rationale: string | null;
  packet_memo: string | null;
}

export interface QueueSnapshot {
  generated_by: string;
  run_date: string;
  attorney_capacity: number;
  label: string;
  succeeded: boolean;
  report: {
    run_id: string;
    committed: string[];
    interrupts: string[];
    refused: string[];
    failures: string[];
    attorney_action: string;
    backstop_used: boolean;
  };
  cases: Case[];
  audit: { seq: number; kind: string; case_id: string | null }[];
  counts: {
    total: number;
    interrupt: number;
    surface_today: number;
    monitor: number;
    hold: number;
    flagged: number;
    audit_events: number;
  };
}

export const BANDS: Record<Level, { label: string; short: string; color: string }> = {
  interrupt: { label: "Interrupt now", short: "L1", color: "var(--color-l1)" },
  surface_today: { label: "Surface today", short: "L2", color: "var(--color-l2)" },
  monitor: { label: "Monitor", short: "L3", color: "var(--color-l3)" },
  hold: { label: "Hold", short: "L0", color: "var(--color-l0)" },
};

export const BAND_ORDER: Level[] = ["interrupt", "surface_today", "monitor", "hold"];

export async function loadQueue(): Promise<QueueSnapshot> {
  const response = await fetch(`${import.meta.env.BASE_URL}queue.json`);
  if (!response.ok) {
    throw new Error(`queue snapshot unavailable (HTTP ${response.status})`);
  }
  return (await response.json()) as QueueSnapshot;
}

/** Human-readable countdown. Overdue is stated as overdue, never as a
 *  negative number: an attorney reads "1 day overdue", not "-1 days". */
export function countdown(days: number | null): string {
  if (days === null) return "no clock";
  if (days < 0) return `${Math.abs(days)} day${Math.abs(days) === 1 ? "" : "s"} overdue`;
  if (days === 0) return "due today";
  return `${days} day${days === 1 ? "" : "s"} left`;
}

export function formatFlag(code: string): string {
  return code.replace(/_/g, " ");
}

/** The live proof endpoint. The door recomputes every answer deadline in the
 *  corpus on each request, so this is a measurement taken while you watch
 *  rather than a number written into the page at build time. */
export interface StatsDivergence {
  case_id: string;
  served: string;
  hand_counted: string;
  hand_counted_weekday: string;
  statutory: string;
  statutory_weekday: string;
  days_off: number;
}

export interface StatsSummons {
  case_id: string;
  computed: string;
  controlling: string;
  authority: string;
}

export interface Stats {
  recomputed_at: string;
  note: string;
  corpus: { cases: number; label: string; run_date: string };
  computation: {
    deadlines_computed: number;
    refused_unverified: number;
    cases_carrying_a_flag: number;
    elapsed_ms: number;
    citation: string;
  };
  headline: {
    answer_deadlines_hand_counting_gets_wrong: number;
    of_deadlines_computed: number;
    why_it_matters: string;
  };
  because_the_deadline_rolls: StatsDivergence[];
  because_the_summons_controls: StatsSummons[];
}

export async function loadStats(): Promise<Stats> {
  const response = await fetch("/api/stats", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  return (await response.json()) as Stats;
}

/* ---------------------------------------------------------------- runs ---
 * The door's run endpoints. A run is two HTTP calls with a human in between:
 * start it, then answer the attorney interrupt it stops at.
 */

export interface AwaitingCase {
  case_id: string;
  rank: number;
  days_remaining: number | null;
  factors: string[];
  flags: string[];
  rationale: string | null;
}

export interface RunStep {
  seq: number;
  kind: string;
  detail?: string;
  case_id?: string | null;
}

export interface RunResult {
  interrupted: boolean;
  status?: string;
  interrupts?: { id: string; name: string | null; reason: unknown }[];
  awaiting?: AwaitingCase[];
  total_cases?: number;
  attorney_action?: string;
  committed?: string[];
  failures?: string[];
  refused?: string[];
  missing_memos?: string[];
  backstop_used?: boolean;
  model_error?: string;
  succeeded?: boolean;
  steps?: RunStep[];
  audit?: RunStep[];
}

export interface RunEnvelope {
  run_id: string;
  status: string;
  result?: RunResult;
}

/** Raised with the door's own words so the UI can render the real reason.
 *
 *  Fields are declared and assigned rather than written as constructor
 *  parameter properties: this project builds with `erasableSyntaxOnly`, which
 *  forbids TypeScript syntax that has to survive into emitted JavaScript.
 */
export class DoorError extends Error {
  status: number;
  body: Record<string, unknown> | null;

  constructor(message: string, status: number, body: Record<string, unknown> | null) {
    super(message);
    this.name = "DoorError";
    this.status = status;
    this.body = body;
  }
}

async function send(path: string, body?: unknown): Promise<RunEnvelope> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
    cache: "no-store",
  });
  let parsed: Record<string, unknown> | null = null;
  try {
    parsed = (await response.json()) as Record<string, unknown>;
  } catch {
    parsed = null;
  }
  if (!response.ok) {
    // The door explains itself (a spend cap, an unconfigured runtime). Carry
    // its words up rather than replacing them with a generic failure.
    const detail =
      (parsed?.detail as string) || (parsed?.error as string) || `HTTP ${response.status}`;
    throw new DoorError(detail, response.status, parsed);
  }
  return parsed as unknown as RunEnvelope;
}

export interface WhatIfFlag {
  code: string;
  reason: string;
  day: string | null;
}

export interface WhatIfTrace {
  day: string;
  label: string;
}

export interface WhatIf {
  service_date: string;
  service_method: string;
  computed_deadline: string | null;
  effective_deadline: string | null;
  deadline_basis: string;
  citation: string;
  flags: WhatIfFlag[];
  trace: WhatIfTrace[];
  court_reopens_on: string | null;
  label: string;
}

export async function loadWhatIf(serviceDate: string): Promise<WhatIf> {
  const params = new URLSearchParams({ service_date: serviceDate });
  const response = await fetch(`/api/what-if?${params.toString()}`, { cache: "no-store" });
  let parsed: Record<string, unknown> | null = null;
  try {
    parsed = (await response.json()) as Record<string, unknown>;
  } catch {
    parsed = null;
  }
  if (!response.ok) {
    const detail =
      (parsed?.detail as string) || (parsed?.error as string) || `HTTP ${response.status}`;
    throw new DoorError(detail, response.status, parsed);
  }
  return parsed as unknown as WhatIf;
}

export function receiptSteps(result: RunResult): RunStep[] {
  const raw = result.steps ?? result.audit ?? [];
  return [...raw].sort((a, b) => a.seq - b.seq);
}

export function startRun(capacity = 2): Promise<RunEnvelope> {
  return send("/api/run", { capacity });
}

export function decideRun(runId: string, answer: string): Promise<RunEnvelope> {
  return send(`/api/run/${encodeURIComponent(runId)}/decision`, { response: answer });
}

/** How the run actually ended, in the attorney's terms rather than HTTP's. */
export type Outcome = "committed" | "deferred" | "unresolved";

export function outcomeOf(result: RunResult): Outcome {
  if (result.succeeded === false) return "unresolved";
  if ((result.committed?.length ?? 0) > 0) return "committed";
  return "deferred";
}
