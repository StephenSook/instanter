import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { EvidencePage } from "../components/EvidencePage";

const STATS = {
  recomputed_at: "2026-08-26T00:00:00Z",
  note: "Recomputed on this request.",
  corpus: { cases: 48, label: "EXAMPLE DATA", run_date: "2026-09-09" },
  computation: {
    deadlines_computed: 46,
    refused_unverified: 2,
    cases_carrying_a_flag: 15,
    elapsed_ms: 1.2,
    citation: "O.C.G.A. 44-7-51(b); O.C.G.A. 1-3-1(d)(3); O.C.G.A. 1-4-1",
  },
  headline: {
    answer_deadlines_hand_counting_gets_wrong: 4,
    of_deadlines_computed: 46,
    why_it_matters: "A missed answer deadline in a dispossessory case is a default judgment.",
  },
  because_the_deadline_rolls: [
    {
      case_id: "26ED00107",
      served: "2026-08-08",
      hand_counted: "2026-08-15",
      hand_counted_weekday: "Saturday",
      statutory: "2026-08-17",
      statutory_weekday: "Monday",
      days_off: 2,
    },
  ],
  because_the_summons_controls: [
    {
      case_id: "26ED00120",
      computed: "2026-09-10",
      controlling: "2026-09-12",
      authority: "O.C.G.A. 44-7-51(b)",
    },
  ],
};

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

describe("EvidencePage", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("prints the door's own lists, not a hardcoded 4 of 46", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(STATS));
    render(<EvidencePage />);
    expect(await screen.findByText("26ED00107")).toBeInTheDocument();
    expect(screen.getByText(/2026-08-17/)).toBeInTheDocument();
    expect(screen.getByText("26ED00120")).toBeInTheDocument();
    expect(document.querySelector('[data-field="summons-controlling"]')?.textContent).toBe(
      "2026-09-12",
    );
  });

  it("shows no number when the door does not answer", async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new Error("HTTP 502"));
    render(<EvidencePage />);
    expect(await screen.findByText(/shows no number when/i)).toBeInTheDocument();
    expect(screen.queryByText("26ED00107")).not.toBeInTheDocument();
  });
});
