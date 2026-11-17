/* A design's work items (Azure DevOps, Jira), on the Tasks tab (library-redesign option A, state 9):
   the design's own item, each task of its split paired with an item (or a button to make one), the
   items made from passages, and a box to link an existing one. States are the tracker's own words,
   read from the Library; when it can't be reached the last read stays on screen, marked as old. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { DaemonClient, DesignWorkItems, TrackerLink } from "../../../api/client";
import type { PlanWorkerDTO } from "../../../api/schema";
import { ago } from "../../../components/ProviderConnect";
import { button, field, label, quiet } from "../../../components/ui";
import { message } from "./chrome";

export type WorkClient = Pick<DaemonClient, "designWorkItems" | "createWorkItems" | "linkWorkItem" | "unlinkWorkItem">;

export interface Work {
  state: DesignWorkItems | null;
  /* Why the last read or write failed, in words; null when it didn't. */
  error: string | null;
  busy: boolean;
  link(item: string, target?: TrackerLink["target"]): Promise<void>;
  create(body: { kind: "design" | "task" | "passage"; task?: string; section?: string; quote?: string; title?: string }): Promise<void>;
  unlink(linkId: string): Promise<void>;
}

/* The design's items, read when it opens and again after each change made here. A reply that arrives
   after the design changed (or the reader closed) is dropped. */
export function useWork(client: WorkClient | undefined, docId: string | undefined, repo?: string): Work | undefined {
  const [state, setState] = useState<DesignWorkItems | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const current = useRef(docId);
  current.current = docId;
  const read = useCallback(async () => {
    if (!client || !docId) return;
    try {
      const got = await client.designWorkItems(docId, repo);
      if (current.current === docId) { setState(got); setError(null); }
    } catch (e) {
      if (current.current === docId) setError(message(e));
    }
  }, [client, docId, repo]);
  useEffect(() => {
    setState(null);
    setError(null);
    void read();
  }, [read]);
  const write = useCallback(async (run: () => Promise<unknown>) => {
    setBusy(true);
    let failed: string | null = null;
    try {
      await run();
    } catch (e) {
      failed = message(e);
    }
    // Read again either way (a create may have made some items before it failed), then say why it failed.
    await read();
    if (current.current === docId) {
      setError(failed);
      setBusy(false);
    }
  }, [docId, read]);
  if (!client || !docId) return undefined;
  return {
    state, error, busy,
    link: (item, target) => write(() => client.linkWorkItem(docId, item, target, repo)),
    create: (body) => write(() => client.createWorkItems(docId, body, repo)),
    unlink: (id) => write(() => client.unlinkWorkItem(docId, id, repo)),
  };
}

function Chip({ item }: { item: TrackerLink }) {
  const dot = item.done ? "bg-pass" : item.state ? "bg-[var(--group-1)]" : "bg-ink-3";
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-sm border border-line px-1.5 font-mono text-[11px] leading-[17px] text-ink-2" data-work-item={item.display} data-stale={item.stale || undefined} title={item.stale ? "Last read earlier: the tracker could not be reached" : undefined}>
      <i className={`size-1.5 rounded-full ${dot}`} />
      {item.state ? `${item.display} · ${item.state}` : item.display}
    </span>
  );
}

const trackerName = (t: TrackerLink["tracker"]) => (t === "jira" ? "Jira" : "Azure DevOps");
const open = (item: TrackerLink) => item.url && <a className={`${quiet} text-xs`} href={item.url} target="_blank" rel="noreferrer">{`Open in ${trackerName(item.tracker)} ↗`}</a>;

export function WorkPanel({ work, split, can }: { work: Work; split: PlanWorkerDTO[]; can: boolean }) {
  const [text, setText] = useState("");
  const [onto, setOnto] = useState("design");
  const { state } = work;
  const items = state?.items ?? [];
  const epic = state?.epic ?? null;
  const forTask = (id: string) => items.find((i) => i.target.kind === "task" && i.target.id === id);
  const passages = items.filter((i) => i.target.kind === "passage" || i.target.kind === "section");
  const missing = split.filter((w) => !forTask(w.id));
  const connected = Object.values(state?.connected ?? {}).some(Boolean);
  const submit = async () => {
    const item = text.trim();
    if (!item) return;
    await work.link(item, onto === "design" ? { kind: "design" } : { kind: "task", id: onto });
    setText("");
  };
  return (
    <div className="grid gap-3.5 border-t border-line px-3.5 py-3" data-testid="doc-work-items" data-state={state ? "ready" : work.error ? "failed" : "pending"} data-unreachable={state?.unreachable || undefined}>
      {work.error && <p className="m-0 text-sap" role="alert">{work.error}</p>}
      {state?.unreachable && <p className="m-0 text-ink-3" data-testid="work-unreachable">{`Couldn't reach ${epic ? trackerName(epic.tracker) : "the tracker"}. These are the states as last read${state.read_at ? ` ${ago(state.read_at)}` : ""}.`}</p>}
      {!state ? (work.error ? null : <p className="m-0 text-ink-3">Reading work items…</p>) : (
        <>
          <div className="grid gap-1.5" data-testid="work-design">
            <span className={`${label} text-[11px]`}>This design</span>
            {epic ? (
              <>
                <span className="flex items-center justify-between gap-2"><b className="font-medium text-ink">Epic</b><Chip item={epic} /></span>
                <span className="text-[11.5px] text-ink-3">{`${epic.type ?? "Item"} · ${state.total ? `${state.done} of ${state.total} others done · ` : ""}${epic.read_at ? `read ${ago(epic.read_at)}` : "not read yet"}`}</span>
                <span className="flex gap-1">{open(epic)}{can && <button type="button" className={`${quiet} text-xs`} disabled={work.busy} onClick={() => void work.unlink(epic.link_id)}>Unlink</button>}</span>
              </>
            ) : can && connected ? (
              <span className="flex flex-wrap items-center gap-2 text-ink-3">
                No item yet.
                <button type="button" className={`${button} sm`} disabled={work.busy} onClick={() => void work.create({ kind: "design" })}>Create epic</button>
              </span>
            ) : <p className="m-0 text-ink-3">{connected ? "No item is linked to this design." : "No tracker is connected for your organization. An admin connects one in Settings › Integrations."}</p>}
          </div>
          {split.length > 0 && (
            <div className="grid gap-2" data-testid="work-tasks">
              <span className="flex items-center gap-2"><span className={`${label} text-[11px]`}>{`Its tasks · ${split.length}`}</span>
                {can && connected && missing.length > 1 && <button type="button" className={`${quiet} text-xs`} disabled={work.busy} onClick={() => void work.create({ kind: "task" })}>{`Create the ${missing.length} missing`}</button>}</span>
              {split.map((w) => {
                const item = forTask(w.id);
                return (
                  <div key={w.id} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-2 gap-y-0.5" data-task={w.id}>
                    <b className="truncate font-medium text-ink">{w.title}</b>
                    {item ? <Chip item={item} /> : can && connected ? <button type="button" className={`${button} sm`} disabled={work.busy} onClick={() => void work.create({ kind: "task", task: w.id })}>Create item</button> : <span className="text-ink-3">No item</span>}
                    {!item && <span className="col-span-full text-[11.5px] text-ink-3">{epic ? `Would be created under ${epic.display} with the task's files and its proving test` : "No item yet"}</span>}
                    {item && <span className="col-span-full flex items-center gap-1 text-[11.5px] text-ink-3">{[item.type, item.linked_by].filter(Boolean).join(" · ")}{open(item)}{can && <button type="button" className={`${quiet} text-xs`} disabled={work.busy} onClick={() => void work.unlink(item.link_id)}>Unlink</button>}</span>}
                  </div>
                );
              })}
            </div>
          )}
          {passages.length > 0 && (
            <div className="grid gap-2" data-testid="work-passages">
              <span className={`${label} text-[11px]`}>{`From passages · ${passages.length}`}</span>
              {passages.map((i) => (
                <div key={i.link_id} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-2">
                  <span className="truncate text-ink">{i.type ?? "Item"}{i.target.id ? ` · from §${i.target.id}` : ""}</span>
                  <Chip item={i} />
                </div>
              ))}
            </div>
          )}
          {can && connected && (
            <form className="grid gap-1.5" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
              <span className={`${label} text-[11px]`}>Link an existing item</span>
              <input className={field} value={text} placeholder="AB#1234, BRN-12 or paste its link" aria-label="Link an existing item" onChange={(e) => setText(e.target.value)} />
              <span className="flex items-center gap-2">
                <select className={field} aria-label="Link it to" value={onto} onChange={(e) => setOnto(e.target.value)}>
                  <option value="design">This design</option>
                  {split.map((w) => <option key={w.id} value={w.id}>{w.title}</option>)}
                </select>
                <button type="submit" className={button} disabled={work.busy || !text.trim()}>Link</button>
              </span>
            </form>
          )}
        </>
      )}
    </div>
  );
}
