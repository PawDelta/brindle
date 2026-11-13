/* Whether agents' panes may be shown: false while the engine is still
   bringing its sessions back (a launch, or a new daemon after a restart),
   when the store's rows may still name windows from before. The rest of the
   app never waits on it: only a provider session's pane does, behind a calm
   "Bringing … back" card (screens/Session/ProviderSurface). `epoch` counts
   the times the panes became ready again, so a pane whose view closed while
   the engine was away opens afresh. Outside the app's gate (tests, a single
   screen) panes are ready. */

import { createContext } from "react";

export interface PanesReady {
  ready: boolean;
  epoch: number;
  /* How far the engine's recovery has come, while it is under way. */
  recovery?: { done: number; total: number } | null;
  /* Session roots the engine has yet to bring back; absent from an engine that doesn't say. */
  pending?: string[] | null;
  /* Session roots back and read after it, whose panes may show before the rest are; kept once all
     are ready, as those panes' views stay open (only another daemon's start empties it). */
  back?: string[];
}

/* Whether this session's panes may show: all of them are ready, or this one is back. */
export const panesReadyFor = (panes: PanesReady, root: string | undefined) =>
  panes.ready || (!!root && !!panes.back?.includes(root));

export const PanesReadyContext = createContext<PanesReady>({ ready: true, epoch: 0 });
