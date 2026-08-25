import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { LiveProof } from "../components/LiveProof";

/** The live-proof strip carries the one number a stranger is invited to check.
 *  Its only real failure mode is showing a figure it did not just fetch. */

const STATS = {
  recomputed_at: "2026-08-25T20:00:00Z",
  note: "Recomputed on this request.",
  corpus: { cases: 48, label: "EXAMPLE DATA", run_date: "2026-09-09" },
  computation: {
    deadlines_computed: 46,
    refused_unverified: 2,
    cases_carrying_a_flag: 15,
    elapsed_ms: 0.8,
    citation: "O.C.G.A. 44-7-51(b)",
  },
  headline: {
    answer_deadlines_hand_counting_gets_wrong: 4,
    of_deadlines_computed: 46,
    why_it_matters: "A missed answer deadline is a default judgment.",
  },
  because_the_deadline_rolls: [{}, {}, {}],
  because_the_summons_controls: [{}],
};

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("LiveProof", () => {
  it("renders the headline and its two mechanisms from the response", async () => {
    fetchMock.mockResolvedValueOnce({ ok: true, status: 200, json: async () => STATS } as Response);
    render(<LiveProof />);
    expect(await screen.findByText("4")).toBeInTheDocument();
    expect(screen.getByText(/3 land on a weekend/)).toBeInTheDocument();
    expect(screen.getByText(/1 state a different date/)).toBeInTheDocument();
    expect(screen.getByText(/0.8 ms/)).toBeInTheDocument();
  });

  it("SHOWS NO NUMBER when the door is unreachable", async () => {
    // A live-proof panel that falls back to a hardcoded figure would be worse
    // than no panel, because the figure is the thing being vouched for.
    fetchMock.mockRejectedValueOnce(new Error("HTTP 502"));
    render(<LiveProof />);
    expect(await screen.findByText(/live recomputation unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/shows no number when it cannot reach one/i)).toBeInTheDocument();
    expect(screen.queryByText("4")).not.toBeInTheDocument();
  });

  it("links the raw endpoint so the count can be checked by hand", async () => {
    fetchMock.mockResolvedValueOnce({ ok: true, status: 200, json: async () => STATS } as Response);
    render(<LiveProof />);
    const link = await screen.findByRole("link", { name: /raw json/i });
    expect(link).toHaveAttribute("href", "/api/stats");
  });
});
