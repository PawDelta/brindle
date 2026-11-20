/* Tells the engine the app's effective theme (PUT /v1/app/theme) at start and on every change, so Claude Code
   panes launch in the matching ANSI theme. Fire-and-forget: an engine without the route, or not answering,
   only leaves panes on the theme they had. */

import { currentAppTheme } from "../components/useAppTheme";
import type { DaemonClient } from "./client";

export function watchAppTheme(client: DaemonClient): () => void {
  let sent: string | null = null;
  const send = () => {
    const theme = currentAppTheme();
    if (theme === sent) return;
    sent = theme;
    client.putAppTheme(theme).catch(() => {
      if (sent === theme) sent = null; // try again at the next change
    });
  };
  send();
  const observer = new MutationObserver(send);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  const media = window.matchMedia?.("(prefers-color-scheme: light)");
  media?.addEventListener?.("change", send);
  return () => {
    observer.disconnect();
    media?.removeEventListener?.("change", send);
  };
}
