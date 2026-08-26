import { useCallback, useEffect, useState } from "react";
import Lenis from "lenis";
import gsap from "gsap";
import { Cabinet } from "./components/Cabinet";
import { LiveProof } from "./components/LiveProof";
import { SweepBanner } from "./components/SweepBanner";
import { RunPanel } from "./components/RunPanel";
import { Packet } from "./components/Packet";
import { FolderLoader } from "./components/FolderLoader";
import { WhatIfCalendar } from "./components/WhatIfCalendar";
import { JudgeWalk } from "./components/JudgeWalk";
import { EvidencePage } from "./components/EvidencePage";
import { loadQueue, type QueueSnapshot } from "./data";

type Route =
  | { name: "cabinet" }
  | { name: "case"; id: string }
  | { name: "judge" }
  | { name: "evidence" };

function CaseView({
  snapshot,
  caseId,
  onOpen,
  onBack,
}: {
  snapshot: QueueSnapshot;
  caseId: string;
  onOpen: (id: string) => void;
  onBack: () => void;
}) {
  const found = snapshot.cases.find((c) => c.case_id === caseId);
  if (!found) {
    return (
      <div className="mx-auto max-w-3xl px-5 py-24">
        <h1 className="display text-4xl">No case {caseId} in this sweep</h1>
        <button type="button" onClick={onBack} className="mt-4 font-mono text-sm underline">
          Back to the cabinet
        </button>
      </div>
    );
  }
  const ordered = [...snapshot.cases].sort((a, b) => a.rank - b.rank);
  const at = ordered.findIndex((c) => c.case_id === found.case_id);
  const neighbours = ordered.slice(at + 1, at + 5);
  return <Packet caseRecord={found} neighbours={neighbours} onOpen={onOpen} onBack={onBack} />;
}

function readRoute(): Route {
  const path = window.location.pathname.replace(/\/$/, "") || "/";
  if (path === "/judge") return { name: "judge" };
  if (path === "/evidence") return { name: "evidence" };
  const hash = window.location.hash.replace(/^#\/?/, "");
  if (hash.startsWith("case/")) return { name: "case", id: decodeURIComponent(hash.slice(5)) };
  return { name: "cabinet" };
}

export default function App() {
  const [snapshot, setSnapshot] = useState<QueueSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [route, setRoute] = useState<Route>(readRoute);
  const [loader, setLoader] = useState(true);
  const dismissLoader = useCallback(() => setLoader(false), []);

  useEffect(() => {
    loadQueue().then(setSnapshot).catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    const onNav = () => {
      setRoute(readRoute());
      window.scrollTo({ top: 0 });
    };
    window.addEventListener("hashchange", onNav);
    window.addEventListener("popstate", onNav);
    return () => {
      window.removeEventListener("hashchange", onNav);
      window.removeEventListener("popstate", onNav);
    };
  }, []);

  // Smooth scroll, driven by the GSAP ticker so scroll-linked motion and
  // tweens share one clock. Disabled outright under reduced motion.
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const lenis = new Lenis({ lerp: 0.075 });
    const tick = (time: number) => lenis.raf(time * 1000);
    gsap.ticker.add(tick);
    gsap.ticker.lagSmoothing(0);
    return () => {
      gsap.ticker.remove(tick);
      lenis.destroy();
    };
  }, []);

  const openCase = (id: string) => {
    window.location.hash = `#/case/${encodeURIComponent(id)}`;
  };
  const back = () => {
    window.location.hash = "#/";
  };

  return (
    <div className="min-h-full">
      {loader && <FolderLoader onDone={dismissLoader} />}
      <header className="sticky top-0 z-50 border-b border-white/10 bg-[var(--color-ground)]/85 backdrop-blur-xl">
        <div className="mx-auto flex max-w-[1400px] items-center justify-between gap-4 px-5 py-3.5 sm:px-10">
          <a href="/" className="display text-[1.15rem] tracking-wide">
            Instanter
          </a>
          <nav className="flex items-center gap-4 font-mono text-[0.62rem] tracking-[0.16em] text-white/60 uppercase">
            <a href="/judge" className="hover:text-white/90">
              Judge
            </a>
            <a href="/evidence" className="hover:text-white/90">
              Evidence
            </a>
            <a href="/" className="hidden hover:text-white/90 sm:inline">
              Queue
            </a>
          </nav>
        </div>
      </header>

      {route.name !== "judge" && route.name !== "evidence" && <LiveProof />}
      {route.name !== "judge" && route.name !== "evidence" && <SweepBanner />}

      <main>
        {error && route.name !== "judge" && route.name !== "evidence" && (
          <div className="mx-auto max-w-3xl px-5 py-24">
            <h1 className="display text-4xl">The queue snapshot did not load</h1>
            <p className="mt-3 font-mono text-sm text-white/60">{error}</p>
            <p className="mt-2 font-mono text-xs text-white/60">
              Regenerate it with: .venv/bin/python scripts/export_queue.py
            </p>
          </div>
        )}

        {route.name === "judge" && <JudgeWalk />}
        {route.name === "evidence" && <EvidencePage />}
        {route.name === "cabinet" && <RunPanel />}
        {route.name === "cabinet" && <WhatIfCalendar />}

        {route.name === "cabinet" && !snapshot && !error && (
          <div className="mx-auto min-h-screen max-w-3xl px-5 py-24">
            <p className="font-mono text-[0.7rem] tracking-[0.2em] text-white/60 uppercase">
              Opening the cabinet
            </p>
          </div>
        )}

        {snapshot && route.name === "cabinet" && (
          <Cabinet snapshot={snapshot} onOpen={openCase} />
        )}
        {snapshot && route.name === "case" && (
          <CaseView snapshot={snapshot} caseId={route.id} onOpen={openCase} onBack={back} />
        )}
      </main>

      <footer className="mx-auto max-w-[1400px] px-5 py-14 sm:px-10">
        <p className="max-w-2xl font-mono text-[0.66rem] leading-relaxed text-white/55">
          Instanter states operative facts for a licensed attorney and never gives legal advice. Case
          records shown here are synthetic and labelled as such; the statute, the court calendar, and
          every computation are real.
        </p>
        <p className="mt-3 font-mono text-[0.66rem] text-white/60">
          <a href="/judge" className="underline decoration-white/50 hover:text-white/80">
            Judge walk
          </a>
          {" · "}
          <a href="/evidence" className="underline decoration-white/50 hover:text-white/80">
            Evidence
          </a>
          {" · "}
          <a href="/privacy.html" className="underline decoration-white/50 hover:text-white/80">
            Privacy policy
          </a>
        </p>
      </footer>
    </div>
  );
}
