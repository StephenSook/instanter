import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { WhatIfCalendar } from "../components/WhatIfCalendar";

/**
 * The paper calendar is a display of the engine, not a second calendar.
 * Stubs return the same answers tests/test_deadline.py pins: weekend roll
 * 2026-08-08 -> 2026-08-17, and Dec 31 trap 2026-12-24 -> 2026-12-31 with
 * court_closed_not_legal_holiday. A third case returns a date the UI has
 * no reason to know, so a hardcoded calendar in the component would fail.
 */

const WEEKEND = {
  service_date: "2026-08-08",
  service_method: "personal",
  computed_deadline: "2026-08-17",
  effective_deadline: "2026-08-17",
  deadline_basis: "computed",
  citation: "O.C.G.A. 44-7-51(b); O.C.G.A. 1-3-1(d)(3); O.C.G.A. 1-4-1",
  flags: [],
  trace: [
    { day: "2026-08-08", label: "day of actual service; not counted (day 0)" },
    { day: "2026-08-15", label: "day 7 (calendar days; intermediates count)" },
    { day: "2026-08-15", label: "Saturday; roll forward" },
    { day: "2026-08-16", label: "Sunday; roll forward" },
    { day: "2026-08-17", label: "last day to answer (statutory computation)" },
  ],
  court_reopens_on: null,
  label: "EXAMPLE DATA",
};

const DEC31 = {
  service_date: "2026-12-24",
  service_method: "personal",
  computed_deadline: "2026-12-31",
  effective_deadline: "2026-12-31",
  deadline_basis: "computed",
  citation: "O.C.G.A. 44-7-51(b); O.C.G.A. 1-3-1(d)(3); O.C.G.A. 1-4-1",
  flags: [
    {
      code: "court_closed_not_legal_holiday",
      reason: "Last day to answer 2026-12-31 is a courthouse closure that is NOT a Georgia legal holiday.",
      day: "2026-12-31",
    },
  ],
  trace: [
    { day: "2026-12-24", label: "day of actual service; not counted (day 0)" },
    { day: "2026-12-31", label: "day 7 (calendar days; intermediates count)" },
    { day: "2026-12-31", label: "last day to answer (statutory computation)" },
  ],
  court_reopens_on: null,
  label: "EXAMPLE DATA",
};

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("WhatIfCalendar", () => {
  it("shows the engine's Monday after a weekend terminal day, not a Saturday", async () => {
    const user = userEvent.setup();
    fetchMock.mockResolvedValueOnce(jsonResponse(WEEKEND));
    render(<WhatIfCalendar />);
    await user.click(screen.getByRole("button", { name: /weekend roll/i }));
    expect(await screen.findByText(/Saturday; roll forward/)).toBeInTheDocument();
    expect(document.querySelector('[data-field="statutory-deadline"]')?.textContent).toBe(
      "2026-08-17",
    );
    expect(screen.queryByText("Court closed")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls[0][0]).toContain("service_date=2026-08-08");
  });

  it("shows 2026-12-31 and the court-closed-not-legal-holiday flag from the engine", async () => {
    const user = userEvent.setup();
    fetchMock.mockResolvedValueOnce(jsonResponse(DEC31));
    render(<WhatIfCalendar />);
    await user.click(screen.getByRole("button", { name: /dec 31 trap/i }));
    expect(await screen.findByText("Court closed")).toBeInTheDocument();
    expect(document.querySelector('[data-field="statutory-deadline"]')?.textContent).toBe(
      "2026-12-31",
    );
    expect(screen.getByText("Court closed")).toBeInTheDocument();
    expect(screen.getByText("Court closed")).toHaveAttribute(
      "data-flag",
      "court_closed_not_legal_holiday",
    );
    expect(fetchMock.mock.calls[0][0]).toContain("service_date=2026-12-24");
  });

  it("prints whatever deadline the door returned, so a local calendar cannot hide", async () => {
    const user = userEvent.setup();
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        ...WEEKEND,
        service_date: "2026-08-10",
        computed_deadline: "2026-01-02",
        effective_deadline: "2026-01-02",
        trace: [{ day: "2026-01-02", label: "last day to answer (statutory computation)" }],
        flags: [],
      }),
    );
    render(<WhatIfCalendar />);
    await user.click(screen.getByRole("button", { name: /weekend roll/i }));
    expect(await screen.findByText(/last day to answer/)).toBeInTheDocument();
    expect(document.querySelector('[data-field="statutory-deadline"]')?.textContent).toBe(
      "2026-01-02",
    );
    expect(screen.queryByText("2026-08-17")).not.toBeInTheDocument();
  });

  it("shows no deadline when the door does not answer", async () => {
    const user = userEvent.setup();
    fetchMock.mockRejectedValueOnce(new Error("HTTP 502"));
    render(<WhatIfCalendar />);
    await user.click(screen.getByRole("button", { name: /weekend roll/i }));
    expect(await screen.findByText(/the engine did not answer/i)).toBeInTheDocument();
    expect(screen.queryByText("2026-08-17")).not.toBeInTheDocument();
  });
});
