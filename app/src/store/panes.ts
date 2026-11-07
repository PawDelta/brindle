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
}

export const PanesReadyContext = createContext<PanesReady>({ ready: true, epoch: 0 });
