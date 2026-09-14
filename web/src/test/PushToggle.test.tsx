import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PushToggle } from "../components/PushToggle";

afterEach(() => {
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, "serviceWorker");
  window.sessionStorage.clear();
});

describe("PushToggle", () => {
  it("does not claim to be subscribed when Push is missing", () => {
    render(<PushToggle />);
    expect(screen.queryByText(/^subscribed$/i)).toBeNull();
    expect(screen.getByText(/ping this device only when a sweep actually stops/i)).toBeInTheDocument();
  });

  it("does not request notification permission before a real visitor interrupt", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("PushManager", class {});
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: {},
    });
    const user = userEvent.setup();
    render(<PushToggle />);
    await user.click(screen.getByRole("button", { name: /notify me of interrupts/i }));
    expect(screen.getByText(/run a live sweep first/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try notifications again/i })).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("offers a retry when an identical subscription is still being admitted", async () => {
    window.sessionStorage.setItem("instanter:last-interrupt-run", "run-123");
    const subscribe = vi.fn().mockResolvedValue({
      toJSON: () => ({
        endpoint: "https://fcm.googleapis.com/fcm/send/x",
        keys: { p256dh: "key", auth: "auth" },
      }),
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ publicKey: "AA" }),
      })
      .mockResolvedValueOnce({
        ok: false,
        json: async () => ({ error: "subscription_busy" }),
      });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("PushManager", class {});
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: { ready: Promise.resolve({ pushManager: { subscribe } }) },
    });

    const user = userEvent.setup();
    render(<PushToggle />);
    await user.click(screen.getByRole("button", { name: /notify me of interrupts/i }));

    expect(await screen.findByText(/subscription is still being saved/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try notifications again/i })).toBeInTheDocument();
  });

  it("clears a stale proof and recovers after a later live sweep", async () => {
    window.sessionStorage.setItem("instanter:last-interrupt-run", "stale-run");
    const subscribe = vi.fn().mockResolvedValue({
      toJSON: () => ({
        endpoint: "https://fcm.googleapis.com/fcm/send/x",
        keys: { p256dh: "key", auth: "auth" },
      }),
    });
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ publicKey: "AA" }) })
        .mockResolvedValueOnce({
          ok: false,
          status: 412,
          json: async () => ({ error: "recent_visitor_interrupt_required" }),
        }),
    );
    vi.stubGlobal("PushManager", class {});
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: { ready: Promise.resolve({ pushManager: { subscribe } }) },
    });

    const user = userEvent.setup();
    render(<PushToggle />);
    await user.click(screen.getByRole("button", { name: /notify me of interrupts/i }));
    expect(await screen.findByText(/run a new live sweep/i)).toBeInTheDocument();
    expect(window.sessionStorage.getItem("instanter:last-interrupt-run")).toBeNull();

    act(() => window.dispatchEvent(new Event("instanter:interrupt-ready")));
    expect(screen.getByRole("button", { name: /notify me of interrupts/i })).toBeInTheDocument();
  });

  it("keeps a server failure retryable even when its body is not JSON", async () => {
    window.sessionStorage.setItem("instanter:last-interrupt-run", "run-123");
    const subscribe = vi.fn().mockResolvedValue({
      toJSON: () => ({
        endpoint: "https://fcm.googleapis.com/fcm/send/x",
        keys: { p256dh: "key", auth: "auth" },
      }),
    });
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ publicKey: "AA" }) })
        .mockResolvedValueOnce({ ok: false, status: 503, json: async () => Promise.reject() }),
    );
    vi.stubGlobal("PushManager", class {});
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: { ready: Promise.resolve({ pushManager: { subscribe } }) },
    });

    const user = userEvent.setup();
    render(<PushToggle />);
    await user.click(screen.getByRole("button", { name: /notify me of interrupts/i }));
    expect(await screen.findByText(/could not save the subscription/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try notifications again/i })).toBeInTheDocument();
  });
});
