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
