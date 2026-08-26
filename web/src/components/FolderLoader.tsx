import { useEffect, useState } from "react";

/**
 * First-paint overlay. Skippable. Does not wrap Sweep / LiveProof / cabinet:
 * those stay mounted underneath. Dismissing it (Skip, or the 1.5s timer)
 * is the only thing that removes the overlay.
 *
 * Under prefers-reduced-motion the folder does not animate; it is just a
 * static sheet with the same Skip control.
 */

const AUTO_MS = 1500;

export function FolderLoader({ onDone }: { onDone: () => void }) {
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReduced(mq.matches);
    const onChange = () => setReduced(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    const id = window.setTimeout(onDone, AUTO_MS);
    return () => window.clearTimeout(id);
  }, [onDone]);

  return (
    <div
      role="dialog"
      aria-label="Opening the cabinet"
      className="fixed inset-0 z-[80] flex items-center justify-center bg-[var(--color-ground)]/92"
    >
      <div
        className={
          reduced
            ? "paper-grain w-[min(28rem,90vw)] rounded-[3px] bg-[var(--color-paper)] p-8 text-[var(--color-ink)]"
            : "paper-grain folder-open w-[min(28rem,90vw)] rounded-[3px] bg-[var(--color-paper)] p-8 text-[var(--color-ink)]"
        }
      >
        <p className="font-mono text-[0.62rem] tracking-[0.2em] text-[var(--color-ink-soft)] uppercase">
          Opening the cabinet
        </p>
        <p className="display mt-3 text-[clamp(2.4rem,8vw,4rem)] leading-none">Instanter</p>
        <p className="mt-3 font-serif text-[1.05rem] leading-snug text-[var(--color-ink-soft)]">
          The morning queue, counted from the statute.
        </p>
        <button
          type="button"
          onClick={onDone}
          className="mt-6 font-mono text-[0.7rem] tracking-[0.16em] text-[var(--color-ink)] uppercase underline decoration-[var(--color-ink-soft)] underline-offset-4"
        >
          Skip
        </button>
      </div>
    </div>
  );
}
