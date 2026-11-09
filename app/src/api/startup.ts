import { invoke, isTauri } from "@tauri-apps/api/core";

type StartupStep = "webview" | "meta" | "snapshot" | "sessions_feed" | "shell_painted" | "panes_ready" | "terminal_attached";

// Native monotonic time starts at the process entry point. Only fixed labels
// cross this route; diagnostics must never carry daemon or session data.
const seen = new Set<StartupStep>();
export function startupStep(step: StartupStep): void {
  if (!isTauri() || seen.has(step)) return;
  seen.add(step);
  void invoke("app_startup_step", { step }).catch(() => {});
}
