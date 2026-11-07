/* A session's visuals, read when the session opens and again on each visual.* event on the bus for
   it (published, answered, kept, removed), and after each change made here (an answer, a Keep, a
   read). Only the newest read for the session on screen applies: a change made here drops every
   read begun before it, so a list read before an answer can't put the unanswered row back. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { VisualDTO } from "../../../api/client";
import type { BusEvent } from "../../../api/events";
import { useAppState } from "../../../store/useStore";
import type { VisualsClient } from "./client";

export interface Visuals {
  /* null until read, and while the engine has no visuals (an older one: the route is missing). */
  visuals: VisualDTO[] | null;
  reload(): void;
  /* A row the engine just answered with (an answer, a Keep): in place at once, then the list read
     afresh, any older read dropped. */
  put(visual: VisualDTO): void;
  /* The engine marked this revision read: its dot goes, then the list is read afresh. */
  read(id: string, rev: number): void;
}

const about = (e: BusEvent, root: string) => e.kind.startsWith("visual.") && (e.root_id === root || e.payload.session === root);

export function useVisuals(client: VisualsClient, root: string): Visuals {
  const [got, setGot] = useState<{ root: string; visuals: VisualDTO[] } | null>(null);
  const seq = useRef(0);
  // The last visual.* event about this session: a new one reads the list again.
  const changed = useAppState((s) => s.recent.filter((e) => about(e, root)).at(-1)?.id ?? 0);
  const reload = useCallback(() => {
    const mine = ++seq.current;
    Promise.resolve()
      .then(() => client.visuals(root))
      .then(
        (visuals) => {
          if (mine === seq.current) setGot({ root, visuals });
        },
        () => {
          // No visuals route (an older engine) or a failed read: nothing to show, no button.
          if (mine === seq.current) setGot(null);
        },
      );
  }, [client, root]);
  useEffect(reload, [reload, changed]);
  // A local change to one row; reloading bumps the sequence, so a read begun before it is dropped.
  const change = useCallback(
    (id: string, edit: (v: VisualDTO) => VisualDTO | null) => {
      setGot((g) => (g && g.root === root ? { ...g, visuals: g.visuals.map((v) => (v.id === id ? (edit(v) ?? v) : v)) } : g));
      reload();
    },
    [root, reload],
  );
  // An answer to an older revision, landing after a newer one was read, doesn't put the old one back.
  const put = useCallback((visual: VisualDTO) => change(visual.id, (v) => (visual.rev >= v.rev ? visual : null)), [change]);
  const read = useCallback((id: string, rev: number) => change(id, (v) => (v.rev === rev ? { ...v, unread: false } : null)), [change]);
  return { visuals: got && got.root === root ? got.visuals : null, reload, put, read };
}

/* Open: still asks the person (or asks nothing and isn't answered). Decided: answered. */
export const decided = (v: VisualDTO): boolean => !!v.answer;
