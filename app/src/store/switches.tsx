/* Switches: a feature the owner can turn on or off for everyone without a
   release. useSwitch("name") is the one way the app reads one. Today each
   reads its default here; a follow-up backs them with the server's
   GET /api/brindle/v1/switches, so a dashboard can flip one with no deploy.
   Never an env var or a build-time constant: those need a release.

   A SwitchesProvider overrides switches below it (a test of the other side). */

import { createContext, useContext, type ReactNode } from "react";

const DEFAULTS = {
  /* The Inbox as a queue you clear (InboxQueue.tsx, design inbox-redesign.html); off: the cards by session. */
  inbox_queue_v2: true,
} as const;

export type SwitchName = keyof typeof DEFAULTS;
type Overrides = Partial<Record<SwitchName, boolean>>;

const SwitchesContext = createContext<Overrides>({});

export function SwitchesProvider({ value, children }: { value: Overrides; children: ReactNode }) {
  return <SwitchesContext.Provider value={value}>{children}</SwitchesContext.Provider>;
}

export function useSwitch(name: SwitchName): boolean {
  return useContext(SwitchesContext)[name] ?? DEFAULTS[name];
}
