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
  /* Every root this engine's recovery has named pending, back since or not: until its pane may show
     (`panesReadyFor`), the rows read so far can't say it is back (the snapshot read before its release
     has no root for it, the feed's may say paused or nothing), so it is restarting, never paused. */
  returning?: string[];
  /* Session roots back and read after it, whose panes may show before the rest are; kept once all
     are ready, as those panes' views stay open (only another daemon's start empties it). */
  back?: string[];
  /* Those of `back` whose sessions-feed row hasn't been read since their release: a paused row there is
     still the last daemon's (it paused them on its way out), not a pause. A resuming row can likewise
     predate the pane's release and must not cover it again. Kept past the end of recovery
     (every session it brought back is marked so then) until a feed read begun after it has answered. */
  stale?: string[];
  /* The engine hasn't answered the gate since the app (re)connected: rows read meanwhile may be a
     recovering engine's, so none of them can say a session is paused. */
  unsure?: boolean;
  /* The sessions this engine's recovery brought back, known back: the liveness sweep looked at their
     panes while they were down, and a look taken then can arrive after their return (the sweep sends
     what it gathered only once its whole tick is done). So a "not alive" for one ends it only once the
     engine, asked after that word came (GET /agents/{id}, which looks at the pane itself), says so too. */
  recovered?: string[];
}

/* Whether this session's panes may show: all of them are ready, or this one is back. */
export const panesReadyFor = (panes: PanesReady, root: string | undefined) =>
  panes.ready || (!!root && !!panes.back?.includes(root));

export const PanesReadyContext = createContext<PanesReady>({ ready: true, epoch: 0 });

/* The session screen says which session is on screen (null: none). Once that one is back after a
   restart, the gate reads its chat alone (GET /sessions/{root}/view) and shows its pane from that,
   rather than wait for a whole snapshot, seconds each while the rest come back. */
export const OpenSessionContext = createContext<(root: string | null) => void>(() => {});
