import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RunPanel } from "../components/RunPanel";

/** These tests exist to stop the panel lying.
 *
 *  The happy path is the least interesting thing here. What matters is that a
 *  failed call renders a failure and not a number, that a refused decision
 *  renders as a FAILED run rather than a success, and that one click cannot
 *  start two runs that each spend model tokens.
 */

const AWAITING = {
  interrupted: true,
  total_cases: 48,
  interrupts: [{ id: "v1:before_tool_call:abc", name: "attorney-approval", reason: {} }],
  awaiting: [
    {
      case_id: "26ED00101",
      rank: 1,
      days_remaining: -1,
      factors: ["deadline passed"],
      flags: [],
      rationale: "The deadline has passed.",
    },
    {
      case_id: "26ED00102",
      rank: 2,
      days_remaining: 0,
      factors: ["due today"],
      flags: [],
      rationale: "Due today.",
    },
  ],
  steps: [
    { seq: 1, kind: "ingest", detail: "48 cases read" },
    { seq: 2, kind: "extract" },
    { seq: 3, kind: "compute" },
    { seq: 4, kind: "rank" },
    { seq: 5, kind: "stop", detail: "attorney interrupt" },
  ],
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

async function startAndAwait() {
  const user = userEvent.setup();
  fetchMock.mockResolvedValueOnce(
    jsonResponse({ run_id: "run-1", status: "awaiting_attorney", result: AWAITING }),
  );
  render(<RunPanel />);
  await user.click(screen.getByRole("button", { name: /sweep the queue/i }));
  await screen.findByText(/need a decision/i);
  return user;
}

describe("starting a run", () => {
  it("shows the cases the attorney is being asked about, from the response", async () => {
    await startAndAwait();
    expect(screen.getByText("26ED00101")).toBeInTheDocument();
    expect(screen.getByText("26ED00102")).toBeInTheDocument();
    // Rendered from the payload, not counted in the UI.
    expect(screen.getByText(/48 swept/)).toBeInTheDocument();
  });

  it("states that nothing is committed until the human answers", async () => {
    await startAndAwait();
    expect(screen.getByText(/nothing is committed until you answer/i)).toBeInTheDocument();
  });

  it("renders an overdue case as overdue, never as a negative number", async () => {
    await startAndAwait();
    expect(screen.getByText(/1 day\(s\) overdue/)).toBeInTheDocument();
    expect(screen.queryByText(/-1 day/)).not.toBeInTheDocument();
  });

  it("cannot start two runs from two clicks, because each one spends tokens", async () => {
    const user = userEvent.setup();
    let resolve!: (v: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>((r) => (resolve = r)));
    render(<RunPanel />);
    const button = screen.getByRole("button", { name: /sweep the queue/i });
    await user.click(button);
    // The control is gone while the run is in flight, so a second click has
    // nothing to hit.
    expect(screen.queryByRole("button", { name: /sweep the queue/i })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    resolve(jsonResponse({ run_id: "r", status: "awaiting_attorney", result: AWAITING }));
    await screen.findByText(/need a decision/i);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("when the door cannot be reached", () => {
  it("renders the failure and no number at all", async () => {
    const user = userEvent.setup();
    fetchMock.mockRejectedValueOnce(new Error("Failed to fetch"));
    render(<RunPanel />);
    await user.click(screen.getByRole("button", { name: /sweep the queue/i }));
    expect(await screen.findByText(/the run did not start/i)).toBeInTheDocument();
    expect(screen.getByText(/failed to fetch/i)).toBeInTheDocument();
    // A panel that invented a plausible count here would be worse than one
    // that fails, so assert the absence.
    expect(screen.queryByText(/need a decision/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/48 swept/)).not.toBeInTheDocument();
  });

  it("says plainly that it shows nothing the door did not return", async () => {
    const user = userEvent.setup();
    fetchMock.mockRejectedValueOnce(new Error("network down"));
    render(<RunPanel />);
    await user.click(screen.getByRole("button", { name: /sweep the queue/i }));
    expect(await screen.findByText(/nothing is shown here that the door did not return/i))
      .toBeInTheDocument();
  });
});

describe("the daily spend cap", () => {
  it("renders a 429 as a deliberate limit with its numbers, not a generic error", async () => {
    const user = userEvent.setup();
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        {
          error: "daily_run_cap_reached",
          runs_today: 201,
          cap: 200,
          detail: "This door invokes a paid model, so live runs are capped per day.",
        },
        429,
      ),
    );
    render(<RunPanel />);
    await user.click(screen.getByRole("button", { name: /sweep the queue/i }));
    expect(await screen.findByText(/daily limit reached/i)).toBeInTheDocument();
    expect(screen.getByText(/capped per day/i)).toBeInTheDocument();
    expect(screen.getByText(/201 started today against a cap of 200/)).toBeInTheDocument();
    // Not the generic failure panel.
    expect(screen.queryByText(/the run did not start/i)).not.toBeInTheDocument();
  });
});

describe("the attorney desk and the receipt", () => {
  it("puts interrupt case ids from the payload on the attorney-desk surface", async () => {
    await startAndAwait();
    const desk = screen.getByRole("region", { name: /attorney desk/i });
    expect(desk).toHaveTextContent("26ED00101");
    expect(desk).toHaveTextContent("26ED00102");
  });

  it("still names those cases on the desk when motion is reduced", async () => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      matches: query.includes("prefers-reduced-motion"),
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia;
    try {
      await startAndAwait();
      const desk = screen.getByRole("region", { name: /attorney desk/i });
      expect(desk).toHaveTextContent("26ED00101");
      expect(desk).toHaveTextContent("26ED00102");
    } finally {
      window.matchMedia = original;
    }
  });

  it("prints the run's own steps in order, not a hardcoded pipeline", async () => {
    await startAndAwait();
    const receipt = screen.getByRole("list", { name: /run receipt/i });
    const kinds = [...receipt.querySelectorAll("li")].map((li) => li.textContent ?? "");
    expect(kinds.map((t) => t.toLowerCase()).join(" ")).toMatch(/ingest[\s\S]*extract[\s\S]*compute[\s\S]*rank[\s\S]*stop/);
    expect(receipt).toHaveTextContent("ingest");
    expect(receipt).toHaveTextContent("extract");
    expect(receipt).toHaveTextContent("48 cases read");
  });
});

describe("answering the interrupt", () => {
  it("approve commits the cases and reports success", async () => {
    const user = await startAndAwait();
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        run_id: "run-1",
        status: "resolved",
        result: {
          interrupted: false,
          attorney_action: "approved",
          committed: ["26ED00101", "26ED00102"],
          failures: [],
          backstop_used: false,
          succeeded: true,
        },
      }),
    );
    await user.click(screen.getByRole("button", { name: /^approve$/i }));
    expect(await screen.findByText("Approved")).toBeInTheDocument();
    expect(screen.getByText("success")).toBeInTheDocument();
    expect(screen.getByText(/26ED00101, 26ED00102/)).toBeInTheDocument();
  });

  it("defer commits nothing, and that is an outcome rather than an error", async () => {
    const user = await startAndAwait();
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        run_id: "run-1",
        status: "resolved",
        result: {
          interrupted: false,
          attorney_action: "deferred",
          committed: [],
          failures: [],
          backstop_used: false,
          succeeded: true,
        },
      }),
    );
    await user.click(screen.getByRole("button", { name: /^defer$/i }));
    expect(await screen.findByText("Deferred")).toBeInTheDocument();
    expect(screen.getByText(/nothing was committed/i)).toBeInTheDocument();
    expect(screen.queryByText(/the run did not start/i)).not.toBeInTheDocument();
  });

  it("A REFUSED ANSWER RENDERS AS A FAILED RUN, never as a success", async () => {
    // The single most important assertion in this file. The door refuses a
    // near-miss reply, the deterministic floor commits the cases for later
    // review, and the run reports failure. A UI that showed this as a tidy
    // success would be undoing the guarantee the whole design exists for.
    const user = await startAndAwait();
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        run_id: "run-1",
        status: "resolved",
        result: {
          interrupted: false,
          attorney_action: "pending",
          committed: ["26ED00101", "26ED00102"],
          failures: ["26ED00101", "26ED00102"],
          backstop_used: true,
          succeeded: false,
        },
      }),
    );
    await user.click(screen.getByRole("button", { name: /unclear answer/i }));

    expect(await screen.findByText("Unresolved")).toBeInTheDocument();
    expect(screen.getByText("FAILURE")).toBeInTheDocument();
    expect(screen.queryByText("success")).not.toBeInTheDocument();
    // It committed rows, and must still not read as approved.
    expect(screen.queryByText("Approved")).not.toBeInTheDocument();
    expect(screen.getByText(/was not accepted as a decision/i)).toBeInTheDocument();
    expect(screen.getByText(/still owed a human decision/i)).toBeInTheDocument();
    expect(screen.getByText(/delivered the sweep/i)).toBeInTheDocument();
  });

  it("disables every answer while one is in flight", async () => {
    const user = await startAndAwait();
    let resolve!: (v: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>((r) => (resolve = r)));
    await user.click(screen.getByRole("button", { name: /^approve$/i }));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^defer$/i })).toBeDisabled();
    });
    expect(screen.getByRole("button", { name: /^approve$/i })).toBeDisabled();
    resolve(
      jsonResponse({
        run_id: "run-1",
        status: "resolved",
        result: { interrupted: false, attorney_action: "deferred", committed: [], succeeded: true },
      }),
    );
    await screen.findByText("Deferred");
  });
});
