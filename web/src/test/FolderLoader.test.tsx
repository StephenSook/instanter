import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FolderLoader } from "../components/FolderLoader";
import App from "../App";

const SNAPSHOT = {
  generated_by: "test",
  run_date: "2026-09-09",
  attorney_capacity: 2,
  label: "EXAMPLE DATA",
  succeeded: true,
  report: {
    run_id: "r",
    committed: [],
    interrupts: [],
    refused: [],
    failures: [],
    attorney_action: "none",
    backstop_used: false,
  },
  cases: [],
  audit: [],
  counts: {
    total: 0,
    interrupt: 0,
    surface_today: 0,
    monitor: 0,
    hold: 0,
    flagged: 0,
    audit_events: 0,
  },
};

function jsonResponse(body: unknown) {
  return { ok: true, status: 200, json: async () => body } as Response;
}

describe("FolderLoader", () => {
  it("is on first paint and Skip removes it", async () => {
    const user = userEvent.setup();
    const onDone = vi.fn();
    render(<FolderLoader onDone={onDone} />);
    expect(screen.getByRole("dialog", { name: /opening the cabinet/i })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^skip$/i }));
    expect(onDone).toHaveBeenCalledTimes(1);
  });
});

describe("App with the loader", () => {
  it("keeps Sweep the queue reachable after Skip", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo) => {
        const url = String(input);
        if (url.includes("/api/stats")) {
          return jsonResponse({
            recomputed_at: "2026-08-26T00:00:00Z",
            note: "Recomputed on this request.",
            corpus: { cases: 48, label: "EXAMPLE DATA", run_date: "2026-09-09" },
            computation: {
              deadlines_computed: 46,
              refused_unverified: 2,
              cases_carrying_a_flag: 15,
              elapsed_ms: 1,
              citation: "O.C.G.A. 44-7-51(b)",
            },
            headline: {
              answer_deadlines_hand_counting_gets_wrong: 4,
              of_deadlines_computed: 46,
              why_it_matters: "x",
            },
            because_the_deadline_rolls: [{}, {}, {}],
            because_the_summons_controls: [{}],
          });
        }
        if (url.includes("/api/awaiting")) {
          return jsonResponse({ awaiting: [], truncated: false });
        }
        if (url.includes("queue.json")) {
          return jsonResponse(SNAPSHOT);
        }
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }),
    );
    render(<App />);
    expect(screen.getByRole("dialog", { name: /opening the cabinet/i })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^skip$/i }));
    expect(screen.queryByRole("dialog", { name: /opening the cabinet/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sweep the queue/i })).toBeInTheDocument();
    vi.unstubAllGlobals();
  });
});
