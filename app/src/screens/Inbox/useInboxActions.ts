/* Runs inbox actions (POST /inbox/{id}/{action}) the moment they are
   clicked: no undo window, no delayed request, no interim state. The card
   acted on leaves the list at once and stays out until the next inbox
   snapshot, which is authoritative (an item the engine still lists, a
   later plan from the same agent, shows again). A decision binds to what
   the card showed: an item the engine has replaced since (its detail,
   reason or request id differ in the inbox as it now stands) isn't acted
   on from the old card. The engine's refusal that nothing waits on it any
   more (a stale plan, no checks waiting, a permission request that moved
   on) drops the card and refetches the inbox; any other refusal brings the
   card back with its message in one line. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DaemonError, type DaemonClient, type InboxAction } from "../../api/client";
import type { InboxItemDTO } from "../../api/schema";
import { actionId, itemKey } from "./items";

/* The engine's 409 wording for "nothing waits on that any more" (service/inbox.answer_permission, agents.approve_plan,
   routes_actions.inbox_action), as against a refusal about the action itself. */
const NO_LONGER = /any more|no longer|no checks waiting|no plan awaiting|was answered|its own prompt|isn't a plan-first|supervisor can decide/;

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

/* Stable identity alone does not identify the decision: a worker can ask
   again under the same card key while its previous answer is pending. */
const decision = (item: InboxItemDTO) => JSON.stringify([
  item.detail, item.reason, item.request_id, item.permission?.request_id,
  item.permission?.digest, item.plan_offer,
]);

export function useInboxActions(client: DaemonClient, refresh: () => Promise<void>, _undoMs: number, inbox: InboxItemDTO[]): InboxActions {
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const inFlight = useRef(new Set<string>());
  /* PR dismissals stay hidden through snapshots received before the ack. */
  const [dismissing, setDismissing] = useState<Map<string, string>>(new Map());
  /* Acknowledged, waiting for the next inbox to say what became of the item. */
  const [settled, setSettled] = useState<Map<string, string>>(new Map());
  const [notes, setNotes] = useState<Record<string, Note>>({});
  const latest = useRef(inbox);
  latest.current = inbox;

  // A fresh inbox after an ack is the truth about the items acted on.
  useEffect(() => {
    setSettled((s) => (s.size === 0 ? s : new Map()));
  }, [inbox]);

  const run = useCallback<InboxActions["run"]>(
    (item, action, body = {}) => {
      const key = itemKey(item);
      const chosen = decision(item);
      if (inFlight.current.has(key)) return;
      setNotes((n) => {
        const { [key]: _, ...rest } = n;
        return rest;
      });
      // The decision binds to what the card showed: an item the engine has since replaced (another plan, other
      // checks, a different request) isn't acted on from the old card; it says so and shows the new one.
      const now = latest.current.find((i) => itemKey(i) === key);
      if (!now || decision(now) !== chosen) {
        setNotes((n) => ({ ...n, [key]: { text: "This changed while you decided; have another look.", failed: true } }));
        return;
      }
      inFlight.current.add(key);
      setBusy((b) => new Set(b).add(key));
      if (action === "pr-not-now") {
        setDismissing((s) => new Map(s).set(key, chosen));
      }
      void client
        .inboxAction(actionId(item), action, body)
        .then(() => {
          setSettled((s) => new Map(s).set(key, chosen));
          void refresh();
        })
        .catch((e: unknown) => {
          // The engine has moved on from the snapshot the card came from: drop it and fetch the truth.
          if (e instanceof DaemonError && e.status === 409 && NO_LONGER.test(e.message)) {
            setSettled((s) => new Map(s).set(key, chosen));
            void refresh();
            return;
          }
          setNotes((n) => ({ ...n, [key]: { text: e instanceof Error ? e.message : String(e), failed: true } }));
        })
        .finally(() => {
          inFlight.current.delete(key);
          setBusy((b) => without(b, key));
          if (action === "pr-not-now") {
            setDismissing((s) => {
              const next = new Map(s);
              next.delete(key);
              return next;
            });
          }
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
    const chosen = decision(item);
    setNotes((n) => {
      const { [key]: _, ...rest } = n;
      return rest;
    });
    setSettled((s) => new Map(s).set(key, chosen));
  }, []);

  const report = useCallback((item: InboxItemDTO, text: string, failed = true) => {
    setNotes((n) => ({ ...n, [itemKey(item)]: { text, failed } }));
  }, []);

  const hidden = useMemo(() => {
    const keys = new Set<string>();
    for (const item of inbox) {
      const key = itemKey(item);
      const shown = decision(item);
      if (settled.get(key) === shown || dismissing.get(key) === shown) keys.add(key);
    }
    return keys;
  }, [inbox, settled, dismissing]);
  return { run, now: run, hidden, busy, notes, dismiss, settle, report };
}
