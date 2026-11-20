/* Tells the engine the app's effective theme (PUT /v1/app/theme) at start and on every change, so Claude Code
   panes launch in the matching ANSI theme. One request at a time: a change made while one is in flight waits,
   and only the latest is sent after it settles, so an older theme can never land last. An engine without the
   route, or not answering, only leaves panes on the theme they had. */

import { currentAppTheme } from "../components/useAppTheme";
import type { DaemonClient } from "./client";

export function watchAppTheme(client: DaemonClient): () => void {
  let confirmed: string | null = null; // what the engine last accepted
  let inFlight = false;
  const flush = () => {
    if (inFlight) return;
    const theme = currentAppTheme();
    if (theme === confirmed) return;
    inFlight = true;
    client.putAppTheme(theme).then(
      () => {
        confirmed = theme;
      },
      () => {},
    ).finally(() => {
      inFlight = false;
      if (currentAppTheme() !== theme) flush(); // changed while this one was in flight
    });
  };
  flush();
  const observer = new MutationObserver(flush);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  const media = window.matchMedia?.("(prefers-color-scheme: light)");
  media?.addEventListener?.("change", flush);
  return () => {
    observer.disconnect();
    media?.removeEventListener?.("change", flush);
  };
}
