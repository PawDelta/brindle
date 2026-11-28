import { createContext, useContext, useState, type ReactNode } from "react";

import type { PlanDeliveryItem as PlanChange } from "../../../api/client";

export const PlanDelivery = createContext<readonly PlanChange[]>([]);
export const ShowPlanChanges = createContext<() => void>(() => {});
const DismissSent = createContext<(id: string) => void>(() => {});

export function QueueCount({ items = [] }: { items?: PlanChange[] }) {
  const count = items.filter(item => item.state === "waiting" || item.state === "sending").length;
  return count ? <span className="text-xs text-ink-3">Offline · {count} waiting to send</span> : null;
}

const PlanRevision = createContext<number | undefined>(undefined);

export function PlanDeliveryProvider({ items, rev, children }: { items: PlanChange[]; rev?: number; children: ReactNode }) {
  const [dismissed, setDismissed] = useState<Set<string>>(() => new Set());
  return <PlanDelivery.Provider value={items.filter(item => !dismissed.has(item.client_id))}>
    <PlanRevision.Provider value={rev}><DismissSent.Provider value={id => setDismissed(old => new Set([...old, id]))}>{children}</DismissSent.Provider></PlanRevision.Provider>
  </PlanDelivery.Provider>;
}

export function useQueuedPick(decision: string) {
  const rev = useContext(PlanRevision);
  return useContext(PlanDelivery).filter(item => item.tail === "/votes" && item.body.rev === rev && item.body.decision === decision).at(-1);
}

export function DeliveryStatus({ item }: { item: PlanChange }) {
  const showChanges = useContext(ShowPlanChanges);
  const dismiss = useContext(DismissSent);
  if (item.state === "refused") return <span data-state="refused" className="grid gap-1 text-xs text-ink-2">
    <strong>Not sent</strong>
    {item.code === "forbidden" ? "You no longer have access to make this change."
      : item.code === "invalid" ? "The Library could not accept this change." : "The plan changed before this could be sent."}
    <button type="button" className="justify-self-start underline" onClick={showChanges}>See what changed</button>
  </span>;
  return <span data-state={item.state} onAnimationEnd={item.state === "sent" ? () => dismiss(item.client_id) : undefined}
    className={`inline-flex items-center gap-1 text-xs text-ink-3 ${item.state === "sent" ? "plan-delivery-sent" : ""}`}>
    <span aria-hidden className="size-2 rounded-full border border-current" />
    {item.state === "sent" ? "Sent" : "Waiting to send"}
  </span>;
}

export function useNoteDelivery(id: number, parent?: number) {
  return useContext(PlanDelivery).find(item =>
    item.body.thread_item === id && item.body.parent === parent);
}

export function NoteStatus({ id, parent }: { id: number; parent?: number }) {
  const item = useNoteDelivery(id, parent);
  return item ? <DeliveryStatus item={item} /> : null;
}

export function NoteText({ id, parent, children }: { id: number; parent?: number; children: ReactNode }) {
  const state = useNoteDelivery(id, parent)?.state;
  return <span data-state={state} className={state === "waiting" || state === "sending"
    ? "text-ink-3 [&_del]:text-ink-3 [&_ins]:text-ink-3" : undefined}>{children}</span>;
}

export function SignOffDelivery({ rev }: { rev: number }) {
  const item = useContext(PlanDelivery).filter(item => item.tail === "/sign-offs" && item.body.rev === rev).at(-1);
  return item ? <DeliveryStatus item={item} /> : null;
}

export function EarlierRefusals({ rev, signOffVersion }: { rev: number; signOffVersion: number }) {
  const earlier = useContext(PlanDelivery).filter(item => item.state === "refused" &&
    ((item.tail === "/votes" && item.body.rev !== rev) ||
     (item.tail === "/sign-offs" && item.body.rev !== signOffVersion)));
  return earlier.map(item => <div key={item.client_id} className="px-4 py-2 text-xs text-ink-2">
    {item.tail === "/votes" ? "Pick" : "Sign-off"} · Revision {item.body.rev}
    <DeliveryStatus item={item} />
  </div>);
}

export function usePendingNotes() {
  return useContext(PlanDelivery).filter(item => item.state !== "sent" && item.body.thread_item === undefined &&
    (item.tail === "/suggestions" || (item.tail === "/comments" && item.body.parent === undefined)));
}

export function PendingNote({ item }: { item: PlanChange }) {
  return <article data-state={item.state}
    className={`grid gap-1 border-b border-line px-3.5 py-3 ${item.state === "waiting" ? "text-ink-3" : "text-ink-2"}`}>
    {item.tail === "/suggestions" && <div><del>{item.body.before}</del> <ins>{item.body.after}</ins></div>}
    <span className="whitespace-pre-wrap break-words">{item.body.text}</span><DeliveryStatus item={item} />
  </article>;
}

export function PendingReplies({ thread }: { thread: number }) {
  const items = useContext(PlanDelivery).filter(item => item.tail === "/comments" && item.body.parent === thread &&
    item.body.thread_item === undefined && item.state !== "sent");
  return items.map(item => <div key={item.client_id} data-state={item.state} className={item.state === "waiting" ? "text-ink-3" : "text-ink-2"}>
    <p className="whitespace-pre-wrap break-words">{item.body.text}</p><DeliveryStatus item={item} />
  </div>);
}
