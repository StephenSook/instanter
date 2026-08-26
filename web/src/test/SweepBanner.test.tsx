import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { SweepBanner } from "../components/SweepBanner";

/**
 * These assert what must NOT happen. The banner is a claim about an agent that
 * ran unattended, so the failure that matters is it asserting one when it does
 * not know.
 */

afterEach(() => vi.unstubAllGlobals());

function door(body: unknown, ok = true, status = 200) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok, status, json: async () => body }),
  );
}

describe("SweepBanner", () => {
  it("says a sweep is waiting only when one actually is", async () => {
    door({
      awaiting: [{ run_id: "a", origin: "scheduled", created_at: 1_700_000_000, cases: 2 }],
    });
    render(<SweepBanner />);
    await waitFor(() => expect(screen.getByText(/2 cases/)).toBeTruthy());
    expect(screen.getByText(/nobody watching/i)).toBeTruthy();
    expect(screen.getByText(/not committed|Nothing in it is committed/i)).toBeTruthy();
  });

  it("does NOT claim a scheduled sweep when the only run was a visitor's", async () => {
    door({
      awaiting: [{ run_id: "a", origin: "visitor", created_at: 1_700_000_000, cases: 5 }],
    });
    render(<SweepBanner />);
    await waitFor(() => expect(screen.getByText(/Nothing is waiting/i)).toBeTruthy());
    // The visitor's five cases must not be attributed to the schedule.
    expect(screen.queryByText(/5 cases/)).toBeNull();
  });

  it("shows NO count when the door cannot be reached", async () => {
    door(null, false, 502);
    render(<SweepBanner />);
    await waitFor(() => expect(screen.getByText(/Cannot reach the door/i)).toBeTruthy());
    expect(screen.queryByText(/waiting on an attorney/i)).toBeNull();
  });

  it("renders the same single row in every state, so it cannot shift the layout", async () => {
    door({ awaiting: [] });
    const { container } = render(<SweepBanner />);
    const before = container.querySelectorAll("div").length;
    await waitFor(() => expect(screen.getByText(/Nothing is waiting/i)).toBeTruthy());
    expect(container.querySelectorAll("div").length).toBe(before);
  });
});

describe("SweepBanner truncation", () => {
  it("does NOT claim nothing is waiting when the list was truncated", async () => {
    door({ awaiting: [], truncated: true });
    render(<SweepBanner />);
    await waitFor(() => expect(screen.getByText(/cannot say whether one is/i)).toBeTruthy());
    // The false claim the pagination bug used to produce.
    expect(screen.queryByText(/Nothing is waiting on an attorney right now/i)).toBeNull();
  });

  it("still says nothing is waiting when the list was complete", async () => {
    door({ awaiting: [], truncated: false });
    render(<SweepBanner />);
    await waitFor(() =>
      expect(screen.getByText(/Nothing is waiting on an attorney right now/i)).toBeTruthy(),
    );
  });
});
