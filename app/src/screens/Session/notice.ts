/* What an action's answer looks like on the session screens: a line, red when it failed. */
export type Notice = { text: string; failed: boolean } | null;

export const failure = (e: unknown): Notice => ({ text: e instanceof Error ? e.message : String(e), failed: true });

/* How a session's state reads: live ones by the supervisor's live status. */
export const STATE_PILL: Record<string, string> = {
  failed: "text-sap",
  paused: "text-ink-3",
  waiting: "text-sap",
  processing: "text-pass",
};
