/* Runs inbox actions (POST /inbox/{id}/{action}) the moment they are
   clicked: no undo window, no delayed request, no interim state. The card
   acted on leaves the list at once and stays out until the next inbox
   snapshot, which is authoritative (an item the engine still lists, a
   later plan from the same agent, shows again). A decision binds to what
   the card showed: an item the engine has replaced since (its detail,
   reason or request id differ in the inbox as it now stands) isn't acted
   on from the old card. The engine's own refusal (a stale plan, no checks
   waiting, a permission request that moved on) brings the card back with
   its message in one line. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { DaemonClient, InboxAction } from "../../api/client";
import type { InboxItemDTO } from "../../api/schema";
import { actionId, itemKey } from "./items";

export interface Note {
  text: string;
  failed: boolean;
}

export interface InboxActions {
  /* Send the action now. */
  run(item: InboxItemDTO, action: InboxAction, body?: Record<string, unknown>): void;
  /* The same (kept for the callers that say so). */
  now(item: InboxItemDTO, action: InboxAction, body?: Record<string, unknown>): void;
  /* Keys of items acted on, to leave out of the list until the next inbox. */
  hidden: Set<string>;
  busy: Set<string>;
  notes: Record<string, Note>;
  /* Forget an item's note (a refusal, once read). */
  dismiss(key: string): void;
  /* An item settled another way than POST /inbox (a setup fix's job ended
     well): leave it out until the next inbox says what became of it. */
  settle(item: InboxItemDTO): void;
  /* One line on an item's card (such a job's failure, in the engine's words). */
  report(item: InboxItemDTO, text: string, failed?: boolean): void;
}

const without = (set: Set<string>, key: string) => {
  const next = new Set(set);
  next.delete(key);
  return next;
};

export function useInboxActions(client: DaemonClient, refresh: () => Promise<void>, _undoMs: number, inbox: InboxItemDTO[]): InboxActions {
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const inFlight = useRef(new Set<string>());
  /* Acknowledged, waiting for the next inbox to say what became of the item. */
  const [settled, setSettled] = useState<Set<string>>(new Set());
  const [notes, setNotes] = useState<Record<string, Note>>({});
  const latest = useRef(inbox);
  latest.current = inbox;

  // A fresh inbox after an ack is the truth about the items acted on.
  useEffect(() => {
    setSettled((s) => (s.size === 0 ? s : new Set()));
  }, [inbox]);

  const run = useCallback<InboxActions["run"]>(
    (item, action, body = {}) => {
      const key = itemKey(item);
      if (inFlight.current.has(key)) return;
      setNotes((n) => {
        const { [key]: _, ...rest } = n;
        return rest;
      });
      // The decision binds to what the card showed: an item the engine has since replaced (another plan, other
      // checks, a different request) isn't acted on from the old card; it says so and shows the new one.
      const now = latest.current.find((i) => itemKey(i) === key);
      if (!now || now.detail !== item.detail || now.reason !== item.reason || now.permission?.request_id !== item.permission?.request_id || now.permission?.digest !== item.permission?.digest) {
        setNotes((n) => ({ ...n, [key]: { text: "This changed while you decided; have another look.", failed: true } }));
        return;
      }
      inFlight.current.add(key);
      setBusy((b) => new Set(b).add(key));
      void client
        .inboxAction(actionId(item), action, body)
        .then(() => {
          setSettled((s) => new Set(s).add(key));
          void refresh();
        })
        .catch((e: unknown) => {
          setNotes((n) => ({ ...n, [key]: { text: e instanceof Error ? e.message : String(e), failed: true } }));
        })
        .finally(() => {
          inFlight.current.delete(key);
          setBusy((b) => without(b, key));
        });
    },
    [client, refresh],
  );

  const dismiss = useCallback((key: string) => {
    setNotes((n) => {
      const { [key]: _, ...rest } = n;
      return rest;
    });
  }, []);

  const settle = useCallback((item: InboxItemDTO) => {
    const key = itemKey(item);
    setNotes((n) => {
      const { [key]: _, ...rest } = n;
      return rest;
    });
    setSettled((s) => new Set(s).add(key));
  }, []);

  const report = useCallback((item: InboxItemDTO, text: string, failed = true) => {
    setNotes((n) => ({ ...n, [itemKey(item)]: { text, failed } }));
  }, []);

  return { run, now: run, hidden: settled, busy, notes, dismiss, settle, report };
}
