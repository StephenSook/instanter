import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { loadQueue } from "../data";

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

describe("loadQueue", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("prefers the live door over the exported snapshot", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({
        generated_by: "door /api/queue",
        cases: [{ case_id: "26ED00101", flags: [] }],
        counts: { total: 1 },
      }),
    );
    const q = await loadQueue();
    expect(q.source).toBe("live");
    expect(q.cases[0].case_id).toBe("26ED00101");
    expect(String(vi.mocked(fetch).mock.calls[0][0])).toContain("/api/queue");
  });

  it("falls back to the snapshot when the door is down", async () => {
    vi.mocked(fetch)
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce(
        jsonResponse({
          generated_by: "export_queue.py",
          cases: [{ case_id: "SNAP", flags: [] }],
        }),
      );
    const q = await loadQueue();
    expect(q.source).toBe("snapshot");
    expect(q.cases[0].case_id).toBe("SNAP");
  });
});
