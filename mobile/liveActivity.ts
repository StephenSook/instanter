import { NativeModules, Platform } from "react-native";

/**
 * Live Activity for the attorney interrupt.
 *
 * Lock-screen copy is only a count: "N waiting on an attorney". UPL forbids
 * case ids on a notification preview. No-ops on Android and when the native
 * module is absent (Expo Go, a binary built before the widget existed).
 */

type Native = {
  start: (waiting: number) => void;
  end: () => void;
};

const native = NativeModules.InstanterLiveActivity as Native | undefined;

export function startWaitingActivity(waiting: number): void {
  if (Platform.OS !== "ios") return;
  try {
    native?.start(waiting);
  } catch {
    /* native module missing */
  }
}

export function endWaitingActivity(): void {
  if (Platform.OS !== "ios") return;
  try {
    native?.end();
  } catch {
    /* native module missing */
  }
}
