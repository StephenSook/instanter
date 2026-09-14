import { useEffect, useState } from "react";

/**
 * Subscribe this browser to attorney-interrupt pings.
 *
 * The notification names no case. A lock-screen preview that carried a
 * case id would be a UPL leak. Fail-closed: if Push is missing, or the
 * door has no VAPID key, we say so and do not pretend to be subscribed.
 */

function urlBase64ToUint8Array(base64: string): Uint8Array {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const raw = atob((base64 + padding).replace(/-/g, "+").replace(/_/g, "/"));
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
  return out;
}

type State =
  | { k: "off" }
  | { k: "ready" }
  | { k: "on" }
  | { k: "needs-run"; detail: string }
  | { k: "blocked"; detail: string };

function initialState(): State {
  if (typeof window === "undefined") return { k: "off" };
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    return { k: "blocked", detail: "This browser does not support Web Push." };
  }
  return { k: "ready" };
}

export function PushToggle() {
  const [state, setState] = useState<State>(initialState);

  useEffect(() => {
    const retryAfterRun = () => {
      setState((current) => (current.k === "needs-run" ? { k: "ready" } : current));
    };
    window.addEventListener("instanter:interrupt-ready", retryAfterRun);
    return () => window.removeEventListener("instanter:interrupt-ready", retryAfterRun);
  }, []);

  async function subscribe() {
    try {
      let runId = "";
      try {
        runId = window.sessionStorage.getItem("instanter:last-interrupt-run") || "";
      } catch {
        // The message below covers privacy modes that disable session storage.
      }
      if (!runId) {
        setState({
          k: "needs-run",
          detail: "Run a live sweep first. A recent attorney interrupt is required to subscribe.",
        });
        return;
      }
      const keyRes = await fetch("/api/push/vapid", { cache: "no-store" });
      const keyBody = (await keyRes.json()) as { publicKey?: string; detail?: string };
      if (!keyRes.ok || !keyBody.publicKey) {
        if (keyRes.status >= 500) {
          setState({
            k: "needs-run",
            detail: "The door could not load notification settings. Try once more.",
          });
          return;
        }
        setState({
          k: "blocked",
          detail: keyBody.detail || "This door has no VAPID key.",
        });
        return;
      }
      // serviceWorker.ready never settles if registration failed, so a bare
      // await here would hang the button forever with no message. Bound it.
      const reg = (await Promise.race([
        navigator.serviceWorker.ready,
        new Promise<null>((resolve) => setTimeout(() => resolve(null), 5000)),
      ])) as ServiceWorkerRegistration | null;
      if (!reg) {
        setState({
          k: "blocked",
          detail: "The service worker did not register, so this browser cannot receive pings.",
        });
        return;
      }
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(keyBody.publicKey) as BufferSource,
      });
      const saved = await fetch("/api/push/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...sub.toJSON(), run_id: runId }),
      });
      if (!saved.ok) {
        if (saved.status >= 500) {
          setState({
            k: "needs-run",
            detail: "The door could not save the subscription. Try once more.",
          });
          return;
        }
        // The door explains its refusals; carry its words up.
        let detail = "The door refused the subscription.";
        try {
          const b = (await saved.json()) as { error?: string; cap?: number };
          if (b.error === "subscription_cap_reached") {
            detail = `The door is at its subscription cap (${b.cap ?? "full"}).`;
          } else if (b.error === "subscription_busy") {
            setState({
              k: "needs-run",
              detail: "The subscription is still being saved. Try once more.",
            });
            return;
          } else if (b.error === "recent_visitor_interrupt_required") {
            window.sessionStorage.removeItem("instanter:last-interrupt-run");
            setState({
              k: "needs-run",
              detail: "That sweep is no longer awaiting a decision. Run a new live sweep.",
            });
            return;
          } else if (b.error) {
            detail = `The door refused the subscription: ${b.error}.`;
          }
        } catch {
          // keep the generic line
        }
        setState({ k: "blocked", detail });
        return;
      }
      setState({ k: "on" });
    } catch (e) {
      setState({
        k: "needs-run",
        detail: e instanceof Error ? e.message : "subscribe failed",
      });
    }
  }

  return (
    <section className="border-b border-white/10 bg-[var(--color-ground)]">
      <div className="mx-auto flex max-w-[1400px] flex-wrap items-center justify-between gap-4 px-5 py-4 sm:px-10">
        <p className="max-w-2xl font-mono text-[0.66rem] leading-relaxed text-white/55">
          Ping this device only when a sweep actually stops for an attorney. The
          notification names no case.
        </p>
        {state.k === "ready" && (
          <button
            type="button"
            onClick={() => void subscribe()}
            className="font-mono text-[0.66rem] tracking-[0.16em] text-white uppercase underline decoration-white/40 underline-offset-4"
          >
            Notify me of interrupts
          </button>
        )}
        {state.k === "on" && (
          <p className="font-mono text-[0.66rem] tracking-[0.16em] text-[var(--color-flag)] uppercase">
            Subscribed
          </p>
        )}
        {state.k === "needs-run" && (
          <div className="flex flex-wrap items-center justify-end gap-3">
            <p className="font-mono text-[0.66rem] text-white/45">{state.detail}</p>
            <button
              type="button"
              onClick={() => void subscribe()}
              className="font-mono text-[0.66rem] tracking-[0.16em] text-white uppercase underline decoration-white/40 underline-offset-4"
            >
              Try notifications again
            </button>
          </div>
        )}
        {state.k === "blocked" && (
          <p className="font-mono text-[0.66rem] text-white/45">{state.detail}</p>
        )}
      </div>
    </section>
  );
}
