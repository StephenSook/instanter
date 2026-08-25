import { useState } from "react";
import {
  ActivityIndicator,
  Linking,
  Pressable,
  ScrollView,
  StatusBar,
  StyleSheet,
  Text,
  View,
} from "react-native";
// React Native's own SafeAreaView is iOS-only and a silent no-op on Android, so
// the wordmark drew straight through the status bar clock and the kicker ran
// through the wifi and battery icons. Android 15 forces edge-to-edge for
// targetSdk 35 and above, which means the insets have to be applied by the app.
// This package is the one that reports them on both platforms.
import { SafeAreaProvider, SafeAreaView } from "react-native-safe-area-context";
import Constants from "expo-constants";

/**
 * Instanter, for the attorney who is not at their desk.
 *
 * This is not a second console. The console is where an operator watches the
 * queue; this is where the one person who is allowed to decide actually
 * decides. An escalation fires while they are in a hallway at the courthouse,
 * and they approve or defer from a phone.
 *
 * It talks to the same public door as the web console. No second backend, no
 * second auth story, and nothing rendered that the door did not return: if a
 * call fails, the failure is what appears, because a decision screen that
 * invents a case would be worse than one that refuses to load.
 */

const DOOR: string =
  (Constants.expoConfig?.extra as { doorUrl?: string } | undefined)?.doorUrl ??
  "https://d2ew2t4uldglcr.cloudfront.net";

type AwaitingCase = {
  case_id: string;
  rank: number;
  days_remaining: number | null;
  factors: string[];
  flags: string[];
  rationale: string | null;
};

type RunResult = {
  interrupted: boolean;
  awaiting?: AwaitingCase[];
  total_cases?: number;
  attorney_action?: string;
  committed?: string[];
  failures?: string[];
  backstop_used?: boolean;
  succeeded?: boolean;
};

type Screen =
  | { k: "idle" }
  | { k: "working"; note: string }
  | { k: "awaiting"; runId: string; result: RunResult }
  | { k: "done"; result: RunResult }
  | { k: "failed"; message: string };

async function post(path: string, body: unknown): Promise<any> {
  const response = await fetch(`${DOOR}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const parsed = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(parsed?.detail || parsed?.error || `HTTP ${response.status}`);
  }
  return parsed;
}

function countdown(days: number | null): string {
  if (days === null) return "no reliable clock";
  if (days < 0) return `${Math.abs(days)} day${Math.abs(days) === 1 ? "" : "s"} overdue`;
  if (days === 0) return "due today";
  return `${days} day${days === 1 ? "" : "s"} left`;
}

export default function App() {
  const [screen, setScreen] = useState<Screen>({ k: "idle" });
  const busy = screen.k === "working";

  async function sweep() {
    if (busy) return; // each run spends model tokens
    setScreen({ k: "working", note: "Reading every case and computing the deadlines." });
    try {
      const env = await post("/api/run", { capacity: 2 });
      const result: RunResult = env.result;
      if (result?.interrupted) setScreen({ k: "awaiting", runId: env.run_id, result });
      else setScreen({ k: "done", result });
    } catch (e) {
      setScreen({ k: "failed", message: e instanceof Error ? e.message : String(e) });
    }
  }

  async function decide(runId: string, answer: string) {
    if (busy) return;
    setScreen({ k: "working", note: "Recording your decision and resuming the run." });
    try {
      const env = await post(`/api/run/${encodeURIComponent(runId)}/decision`, {
        response: answer,
      });
      setScreen({ k: "done", result: env.result });
    } catch (e) {
      setScreen({ k: "failed", message: e instanceof Error ? e.message : String(e) });
    }
  }

  return (
    <SafeAreaProvider>
      <SafeAreaView style={s.safe} edges={["top", "bottom", "left", "right"]}>
        <StatusBar barStyle="light-content" backgroundColor="transparent" translucent />
        <View style={s.header}>
          <Text style={s.wordmark}>INSTANTER</Text>
          <Text style={s.kicker}>ATTORNEY REVIEW</Text>
        </View>

        <ScrollView contentContainerStyle={s.scroll}>
          {screen.k === "idle" && (
            <View>
              <Text style={s.h1}>The morning queue</Text>
              <Text style={s.body}>
                Every case is read, every answer deadline is computed from the statute, and the
                queue is ranked by how close it is to a default writ. Then it stops and asks you.
              </Text>
              <Pressable style={s.primary} onPress={sweep} accessibilityRole="button">
                <Text style={s.primaryText}>SWEEP THE QUEUE</Text>
              </Pressable>
              <Text style={s.fine}>
                Case records are synthetic and labelled as such. The statute, the court calendar,
                and every computation are real.
              </Text>
              <Text
                style={s.link}
                accessibilityRole="link"
                onPress={() => Linking.openURL(`${DOOR}/privacy.html`)}
              >
                Privacy policy
              </Text>
            </View>
          )}

          {screen.k === "working" && (
            <View style={s.center}>
              <ActivityIndicator size="large" color="#c2352b" />
              <Text style={[s.body, s.centerText]}>{screen.note}</Text>
            </View>
          )}

          {screen.k === "awaiting" && (
            <View>
              <Text style={s.kickerRed}>STOPPED FOR YOU</Text>
              <Text style={s.h1}>
                {screen.result.awaiting?.length ?? 0} case
                {(screen.result.awaiting?.length ?? 0) === 1 ? "" : "s"} need a decision
              </Text>
              <Text style={s.fine}>
                {screen.result.total_cases} swept. Nothing is committed until you answer.
              </Text>

              {(screen.result.awaiting ?? []).map((c) => (
                <View key={c.case_id} style={s.card}>
                  <View style={s.cardTop}>
                    <Text style={s.caseId}>{c.case_id}</Text>
                    <Text
                      style={[
                        s.countdown,
                        (c.days_remaining ?? 0) < 0 ? s.countdownUrgent : undefined,
                      ]}
                    >
                      RANK {c.rank} · {countdown(c.days_remaining).toUpperCase()}
                    </Text>
                  </View>
                  {c.rationale ? <Text style={s.rationale}>{c.rationale}</Text> : null}
                </View>
              ))}

              <Pressable
                style={s.approve}
                onPress={() => decide(screen.runId, "approve")}
                accessibilityRole="button"
              >
                <Text style={s.approveText}>APPROVE</Text>
              </Pressable>
              <Pressable
                style={s.defer}
                onPress={() => decide(screen.runId, "defer: reviewing at the office")}
                accessibilityRole="button"
              >
                <Text style={s.deferText}>DEFER</Text>
              </Pressable>
            </View>
          )}

          {screen.k === "done" && <Done result={screen.result} onAgain={() => setScreen({ k: "idle" })} />}

          {screen.k === "failed" && (
            <View>
              <Text style={s.kickerRed}>THE RUN DID NOT COMPLETE</Text>
              <Text style={s.body}>{screen.message}</Text>
              <Text style={s.fine}>Nothing is shown here that the door did not return.</Text>
              <Pressable style={s.primary} onPress={() => setScreen({ k: "idle" })}>
                <Text style={s.primaryText}>START OVER</Text>
              </Pressable>
            </View>
          )}
        </ScrollView>
      </SafeAreaView>
    </SafeAreaProvider>
  );
}

function Done({ result, onAgain }: { result: RunResult; onAgain: () => void }) {
  const committed = result.committed ?? [];
  const unresolved = result.succeeded === false;
  const stamp = unresolved ? "UNRESOLVED" : committed.length > 0 ? "APPROVED" : "DEFERRED";
  const tone = unresolved ? "#e8c547" : committed.length > 0 ? "#c2352b" : "#d9d4c8";
  const line = unresolved
    ? "That answer was not accepted as a decision. Because nobody actually resolved these cases, the deterministic floor committed them for later review and the run reports failure."
    : committed.length > 0
      ? `Committed for review: ${committed.join(", ")}. Each carries a cover memo whose figures the system generated.`
      : "Nothing was committed. The cases stay on the queue, still owed a decision.";

  return (
    <View>
      <View style={[s.stamp, { borderColor: tone }]}>
        <Text style={[s.stampText, { color: tone }]}>{stamp}</Text>
      </View>
      <Text style={s.body}>{line}</Text>
      <View style={s.grid}>
        {[
          ["ATTORNEY ACTION", result.attorney_action || "none"],
          ["COMMITTED", String(committed.length)],
          ["RUN REPORTS", result.succeeded ? "success" : "FAILURE"],
          ["FLOOR", result.backstop_used ? "delivered" : "not needed"],
        ].map(([k, v]) => (
          <View key={k} style={s.gridCell}>
            <Text style={s.gridLabel}>{k}</Text>
            <Text style={s.gridValue}>{v}</Text>
          </View>
        ))}
      </View>
      <Pressable style={s.primary} onPress={onAgain}>
        <Text style={s.primaryText}>ANOTHER SWEEP</Text>
      </Pressable>
    </View>
  );
}

const INK = "#141414";
const PAPER = "#faf6f0";

const s = StyleSheet.create({
  safe: { flex: 1, backgroundColor: INK },
  header: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    paddingHorizontal: 20,
    paddingVertical: 14,
    borderBottomWidth: 1,
    borderBottomColor: "rgba(255,255,255,0.12)",
  },
  wordmark: { color: PAPER, fontSize: 18, fontWeight: "800", letterSpacing: 1 },
  kicker: { color: "rgba(255,255,255,0.6)", fontSize: 10, letterSpacing: 2 },
  kickerRed: { color: "#e07a70", fontSize: 11, letterSpacing: 2, marginBottom: 8 },
  scroll: { padding: 20, paddingBottom: 56 },
  h1: { color: PAPER, fontSize: 30, fontWeight: "800", lineHeight: 34, marginBottom: 10 },
  body: { color: "rgba(255,255,255,0.86)", fontSize: 16, lineHeight: 23, marginBottom: 14 },
  fine: { color: "rgba(255,255,255,0.6)", fontSize: 12, lineHeight: 18, marginTop: 12 },
  link: {
    color: "rgba(255,255,255,0.75)",
    fontSize: 12,
    marginTop: 14,
    textDecorationLine: "underline",
  },
  center: { alignItems: "center", paddingVertical: 60, gap: 18 },
  centerText: { textAlign: "center" },
  primary: {
    backgroundColor: PAPER,
    paddingVertical: 16,
    borderRadius: 3,
    alignItems: "center",
    marginTop: 18,
  },
  primaryText: { color: INK, fontWeight: "700", letterSpacing: 2, fontSize: 13 },
  card: {
    backgroundColor: PAPER,
    borderRadius: 3,
    padding: 14,
    marginTop: 12,
  },
  cardTop: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  caseId: { color: INK, fontWeight: "700", fontSize: 15 },
  countdown: { color: "#6b6b6b", fontSize: 10, letterSpacing: 1 },
  countdownUrgent: { color: "#b3271e", fontWeight: "700" },
  rationale: { color: INK, fontSize: 14, lineHeight: 20, marginTop: 8 },
  approve: {
    borderWidth: 2,
    borderColor: "#c2352b",
    paddingVertical: 16,
    borderRadius: 3,
    alignItems: "center",
    marginTop: 22,
  },
  approveText: { color: "#e07a70", fontWeight: "800", letterSpacing: 2, fontSize: 14 },
  defer: {
    borderWidth: 2,
    borderColor: "rgba(255,255,255,0.6)",
    paddingVertical: 16,
    borderRadius: 3,
    alignItems: "center",
    marginTop: 10,
  },
  deferText: { color: PAPER, fontWeight: "800", letterSpacing: 2, fontSize: 14 },
  stamp: {
    borderWidth: 3,
    borderRadius: 3,
    paddingHorizontal: 14,
    paddingVertical: 6,
    alignSelf: "flex-start",
    transform: [{ rotate: "-6deg" }],
    marginBottom: 20,
  },
  stampText: { fontSize: 20, fontWeight: "800", letterSpacing: 3 },
  grid: { flexDirection: "row", flexWrap: "wrap", marginTop: 8 },
  gridCell: { width: "50%", marginBottom: 14 },
  gridLabel: { color: "rgba(255,255,255,0.6)", fontSize: 10, letterSpacing: 1.5 },
  gridValue: { color: PAPER, fontSize: 15, marginTop: 2 },
});
