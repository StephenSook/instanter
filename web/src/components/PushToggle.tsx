import { useState } from "react";

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

  async function subscribe() {
    try {
      const keyRes = await fetch("/api/push/vapid", { cache: "no-store" });
      const keyBody = (await keyRes.json()) as { publicKey?: string; detail?: string };
      if (!keyRes.ok || !keyBody.publicKey) {
        setState({
          k: "blocked",
          detail: keyBody.detail || "This door has no VAPID key.",
        });
        return;
      }
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(keyBody.publicKey) as BufferSource,
      });
      const saved = await fetch("/api/push/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sub.toJSON()),
      });
      if (!saved.ok) {
        setState({ k: "blocked", detail: "The door refused the subscription." });
        return;
      }
      setState({ k: "on" });
    } catch (e) {
      setState({
        k: "blocked",
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
        {state.k === "blocked" && (
          <p className="font-mono text-[0.66rem] text-white/45">{state.detail}</p>
        )}
      </div>
    </section>
  );
}
