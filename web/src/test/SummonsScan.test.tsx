import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SummonsScan } from "../components/SummonsScan";

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

describe("SummonsScan", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("prints the engine deadline from a transcribed service date", async () => {
    const user = userEvent.setup();
    vi.mocked(fetch)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        blob: async () => new Blob(["png"], { type: "image/png" }),
        json: async () => ({}),
      } as Response)
      .mockResolvedValueOnce(
        jsonResponse({
          extracted: {
            service_date: "2026-08-08",
            service_method: "personal",
            summons_stated_deadline: null,
            case_id: "EX",
          },
          computed_deadline: "2026-08-17",
          effective_deadline: "2026-08-17",
          citation: "O.C.G.A. 44-7-51(b)",
          flags: [],
          trace: [{ day: "2026-08-17", label: "last day to answer (statutory computation)" }],
          label: "EXAMPLE DATA",
        }),
      );
    render(<SummonsScan />);
    await user.click(screen.getByRole("button", { name: /sample summons/i }));
    expect(await screen.findByText("2026-08-17")).toBeInTheDocument();
    expect(document.querySelector('[data-field="ocr-deadline"]')?.textContent).toBe("2026-08-17");
    const ocrCall = vi.mocked(fetch).mock.calls[1];
    expect(String(ocrCall[0])).toContain("/api/ocr");
  });

  it("shows no deadline when the door refuses", async () => {
    const user = userEvent.setup();
    vi.mocked(fetch)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        blob: async () => new Blob(["png"], { type: "image/png" }),
        json: async () => ({}),
      } as Response)
      .mockResolvedValueOnce(
        jsonResponse({ error: "summons_unreadable", detail: "not a summons" }, 422),
      );
    render(<SummonsScan />);
    await user.click(screen.getByRole("button", { name: /sample summons/i }));
    expect(await screen.findByText(/no deadline is shown/i)).toBeInTheDocument();
    expect(document.querySelector('[data-field="ocr-deadline"]')).toBeNull();
  });
});
