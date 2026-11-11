/* The design doc's review column (PlanReader.tsx): what is left to decide as a checklist, then
   the open threads as cards, each tied to its place in the doc: a comment with its replies, or a
   suggestion carrying its before and after so Accept can be judged from the card. Resolved holds
   what was answered; Tasks, the split. The column collapses to a slim strip at the window's edge
   that keeps the two counts (open comments, decisions still to take) and opens it again. */

import { useState, type FormEvent, type ReactNode } from "react";
import type { Plan, PlanAnchor } from "../../../api/client";
import type { PlanThreadDTO } from "../../../api/schema";
import { ChevronRightIcon, CommentIcon } from "../../../components/icons";
import { button, field, label, quiet } from "../../../components/ui";
import { Avatar, Tasks, personName } from "./PlanCards";

export type ReviewTab = "review" | "resolved" | "tasks";

/* What the person is writing: a comment or a question on an anchor, or a suggested replacement
   for a range of a section. `where` names the place, as its card will. */
export type Draft =
  | { kind: "comment" | "ask"; anchor: PlanAnchor; where: string }
  | { kind: "suggest"; section: string; start: number; before: string; where: string };

export interface TodoItem {
  id: string;
  title: string;
  state: "done" | "current" | "todo";
}

export interface ThreadActions {
  reply(thread: number, text: string): Promise<boolean>;
  resolve(thread: number, resolved: boolean): Promise<boolean>;
  accept(thread: number): Promise<boolean>;
  reject(thread: number): Promise<boolean>;
}

/* What someone reviewing another person's design may do to a thread, as the Library says.
   Absent for the doc's author: everything. */
export interface ThreadLimits {
  reply: boolean;
  resolve(thread: PlanThreadDTO): boolean;
}

const card = "grid gap-1.5 border-b border-line px-3.5 py-3 text-ink-2";
const onLine = "font-mono text-[11px] text-ink-3";
const act = "cursor-pointer text-xs text-ink-3 hover:text-ink disabled:cursor-not-allowed disabled:opacity-45";
const box = "size-[11px] flex-none rounded-sm border-[1.5px]";
const clip = (text: string, most = 120) => (text.length > most ? `${text.slice(0, most - 1)}…` : text);

/* A suggestion's change: what goes, then what comes. */
function Change({ before, after }: { before: string; after: string }) {
  return (
    <span className="justify-self-start whitespace-pre-wrap break-words rounded-sm bg-surface-2 px-1.5 py-[3px] font-mono text-[11.5px]" data-testid="suggestion-change">
      {before && <del className="bg-fail-soft text-fail">{clip(before)}</del>}
      {before && after && " → "}
      {after && <ins className="bg-pass-soft text-pass no-underline">{clip(after)}</ins>}
    </span>
  );
}

function Who({ who, supervisor = false }: { who: string; supervisor?: boolean }) {
  return (
    <div className="flex items-center gap-1.5">
      <Avatar who={supervisor ? "supervisor" : who} />
      <b className="font-medium text-ink">{supervisor ? (who === "person" ? "Your supervisor" : `${personName(who)}’s supervisor`) : personName(who)}</b>
    </div>
  );
}

function Thread({ thread: t, where, busy, can, limits, actions, onShow }: {
  thread: PlanThreadDTO;
  where: string;
  busy: boolean;
  /* Whether a suggestion can still be accepted (the plan is open). */
  can: boolean;
  limits?: ThreadLimits;
  actions: ThreadActions;
  onShow(): void;
}) {
  const [replying, setReplying] = useState(false);
  const [text, setText] = useState("");
  const open = t.status === "open";
  const suggestion = t.kind === "suggestion";
  const canJudge = !limits;
  const canResolve = !limits || limits.resolve(t);
  const send = async (e: FormEvent) => {
    e.preventDefault();
    if (!text.trim() || !(await actions.reply(t.id, text.trim()))) return;
    setText("");
    setReplying(false);
  };
  return (
    <article className={`${card} ${open ? "" : "opacity-55"}`} data-testid={`thread-${t.id}`} data-status={t.status} data-kind={t.kind} data-question={t.question || undefined}>
      <button type="button" className={`${onLine} cursor-pointer text-left hover:text-ink-2`} title="Show it in the doc" onClick={onShow}>
        {suggestion && <b className="font-medium uppercase tracking-[0.06em] text-ink-2">Suggestion</b>}
        {suggestion ? ` · ${where}` : `on ${where}`}
        {!open && ` · ${t.status}`}
        {open && t.anchor.state === "detached" && " · that text has since changed"}
      </button>
      <Who who={t.author} />
      {suggestion && <Change before={t.before ?? ""} after={t.after ?? ""} />}
      {t.text && <span className="whitespace-pre-wrap break-words">{t.text}</span>}
      {t.attachments?.map((name) => (
        <span key={name} className="justify-self-start whitespace-nowrap rounded-sm border border-line px-1.5 py-px font-mono text-[11px] text-ink-2">{name}</span>
      ))}
      {!!t.replies?.length && (
        <div className="ml-[9px] grid gap-1.5 border-l border-line-strong pl-2.5">
          {t.replies.map((r) => (
            <div key={r.id} className="grid gap-1.5" data-from-supervisor={r.from_supervisor || undefined}>
              <Who who={r.from_supervisor ? t.author : r.author} supervisor={r.from_supervisor} />
              <span className="whitespace-pre-wrap break-words">{r.text}</span>
            </div>
          ))}
        </div>
      )}
      {replying && (
        <form className="flex items-center gap-2" onSubmit={(e) => void send(e)}>
          <input className={`${field} min-w-0 flex-1 text-[12.5px]`} aria-label="Reply" placeholder="Reply…" autoFocus value={text} onChange={(e) => setText(e.target.value)} />
          <button type="submit" className={`${button} text-xs`} disabled={busy || !text.trim()}>Send</button>
        </form>
      )}
      <div className="mt-0.5 flex gap-3.5">
        {open && suggestion && canJudge && <button type="button" className={`${act} font-medium !text-pass`} disabled={busy || !can || t.anchor.state === "detached"} onClick={() => void actions.accept(t.id)}>Accept</button>}
        {open && suggestion && canJudge && <button type="button" className={act} disabled={busy} onClick={() => void actions.reject(t.id)}>Reject</button>}
        {open && (!limits || limits.reply) && <button type="button" className={act} onClick={() => setReplying((r) => !r)}>Reply</button>}
        {open && !suggestion && canResolve && <button type="button" className={act} disabled={busy} onClick={() => void actions.resolve(t.id, true)}>Resolve</button>}
        {t.status === "resolved" && canResolve && <button type="button" className={act} disabled={busy} onClick={() => void actions.resolve(t.id, false)}>Reopen</button>}
      </div>
    </article>
  );
}

const draftWords = {
  comment: { name: "Comment", placeholder: "What should change here?", send: "Comment" },
  ask: { name: "Ask the supervisor", placeholder: "What do you want to know?", send: "Ask" },
  suggest: { name: "Why (optional)", placeholder: "Why (optional)", send: "Suggest" },
};

function DraftCard({ draft, stale, busy, onSend, onCancel }: { draft: Draft; stale: boolean; busy: boolean; onSend(text: string, after: string): void; onCancel(): void }) {
  const [text, setText] = useState("");
  const [after, setAfter] = useState(draft.kind === "suggest" ? draft.before : "");
  const words = draftWords[draft.kind];
  const ready = !stale && (draft.kind === "suggest" ? after !== draft.before : !!text.trim());
  const send = () => {
    if (ready && !busy) onSend(text.trim(), after);
  };
  return (
    <form className={`${card} bg-canvas-2`} aria-label={draft.kind === "suggest" ? "New suggestion" : "New comment"} data-testid="thread-draft" data-stale={stale || undefined}
      onSubmit={(e) => { e.preventDefault(); send(); }}
      onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send(); }}>
      <span className={onLine}>
        {draft.kind === "suggest" && <b className="font-medium uppercase tracking-[0.06em] text-ink-2">Suggestion</b>}
        {draft.kind === "suggest" ? ` · ${draft.where}` : `on ${draft.where}`}
      </span>
      {draft.kind === "suggest" && (
        <>
          <Change before={draft.before} after="" />
          <textarea className={`${field} min-h-16 w-full text-[12.5px]`} aria-label="Replace with" autoFocus value={after} onChange={(e) => setAfter(e.target.value)} />
        </>
      )}
      <textarea className={`${field} ${draft.kind === "suggest" ? "min-h-8" : "min-h-16"} w-full text-[12.5px]`} aria-label={words.name} placeholder={words.placeholder} autoFocus={draft.kind !== "suggest"} value={text} onChange={(e) => setText(e.target.value)} />
      {stale && <p role="alert" className="text-xs text-sap">This part of the doc changed in a newer revision. Cancel, and start again on the new text.</p>}
      <div className="flex items-center gap-1.5">
        <button type="submit" className={`${button} text-xs`} disabled={!ready || busy}>{words.send}</button>
        <button type="button" className={`${quiet} text-xs`} onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

export function Review({ plan, tab, onTab, todo, decided, onJump, where, draft, draftStale, busy, onDraftSend, onDraftCancel, actions, limits, onShow, shared, onCollapse }: {
  plan: Plan;
  limits?: ThreadLimits;
  tab: ReviewTab;
  onTab(tab: ReviewTab): void;
  todo: TodoItem[];
  decided: number;
  onJump(decision: string): void;
  /* A thread's place in the doc, in words. */
  where(thread: PlanThreadDTO): string;
  draft: Draft | null;
  /* What the draft is on changed in a newer revision: it can't be sent. */
  draftStale: boolean;
  busy: boolean;
  onDraftSend(text: string, after: string): void;
  onDraftCancel(): void;
  actions: ThreadActions;
  /* Bring a thread's place in the doc into view. */
  onShow(thread: PlanThreadDTO): void;
  shared: Map<string, string[]>;
  onCollapse(): void;
}) {
  const threads = plan.threads ?? [];
  const open = threads.filter((t) => t.status === "open");
  const answered = threads.filter((t) => t.status !== "open");
  const tabs: [ReviewTab, string, number | null][] = [["review", "Review", open.length], ["resolved", "Resolved", answered.length], ["tasks", "Tasks", null]];
  const shown = tab === "review" ? open : tab === "resolved" ? answered : [];
  let body: ReactNode = null;
  if (tab === "tasks") {
    body = (
      <div className="flex flex-col gap-2.5 px-3.5 py-3" data-testid="plan-tasks">
        {plan.split.length === 0 ? <p className="text-ink-3">This design has no split yet.</p>
          : plan.state === "approved" ? <p className="text-ink-3">Its tasks are the live board above.</p>
          : <Tasks plan={plan} shared={shared} />}
      </div>
    );
  } else if (shown.length === 0 && !(tab === "review" && draft)) {
    body = <p className="px-3.5 py-3 text-ink-3">{tab === "review" ? "No open comments. Highlight any text in the doc to comment on it." : "Nothing resolved yet."}</p>;
  }
  return (
    <aside aria-label="Review" className="flex w-[304px] shrink-0 flex-col border-l border-line text-[12.5px]" data-testid="plan-review">
      <div className="flex items-center gap-4 border-b border-line pl-3.5 pr-1.5 text-ink-3">
        <div role="tablist" aria-label="Review column" className="flex min-w-0 flex-1 gap-4">
          {tabs.map(([id, name, count]) => (
            <button key={id} type="button" role="tab" aria-selected={tab === id} className={`-mb-px cursor-pointer border-b-2 pb-2 pt-2.5 ${tab === id ? "border-branch font-semibold text-ink" : "border-transparent hover:text-ink-2"}`} onClick={() => onTab(id)}>
              {name}
              {count !== null && <small className="ml-1 font-mono text-[11px] font-normal text-ink-3">{count}</small>}
            </button>
          ))}
        </div>
        <button type="button" className={`${quiet} px-1`} aria-label="Collapse the review column" aria-expanded title="Collapse the review column" data-testid="plan-review-collapse" onClick={onCollapse}>
          <ChevronRightIcon size={14} />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {tab === "review" && todo.length > 0 && (
          <div className="grid gap-1.5 border-b border-line px-3.5 py-3" data-testid="plan-todo">
            <div className="mb-0.5 flex items-center justify-between">
              <span className={`${label} text-[11px]`}>{`To decide · ${decided} of ${todo.length}`}</span>
              <span className="inline-flex items-center gap-[3px]" aria-hidden>
                {todo.map((d) => <i key={d.id} className={`h-[3px] w-[18px] rounded-sm ${d.state === "done" ? "bg-pass" : d.state === "current" ? "bg-ink-2" : "bg-line-strong"}`} />)}
              </span>
            </div>
            {todo.map((d) => (
              <button key={d.id} type="button" data-state={d.state} aria-current={d.state === "current" ? "step" : undefined} onClick={() => onJump(d.id)}
                className={`flex cursor-pointer items-center gap-2 text-left hover:text-ink ${d.state === "current" ? "font-semibold text-ink" : d.state === "done" ? "text-ink-3 line-through" : "text-ink-2"}`}>
                <i className={`${box} ${d.state === "done" ? "border-pass bg-pass" : d.state === "current" ? "border-branch" : "border-line-strong"}`} />
                {d.title}
              </button>
            ))}
          </div>
        )}
        {tab === "review" && draft && <DraftCard key={`${draft.kind}:${draft.where}`} draft={draft} stale={draftStale} busy={busy} onSend={onDraftSend} onCancel={onDraftCancel} />}
        {shown.map((t) => <Thread key={t.id} thread={t} where={where(t)} busy={busy} can={plan.state === "open"} limits={limits} actions={actions} onShow={() => onShow(t)} />)}
        {body}
      </div>
    </aside>
  );
}

/* The column, collapsed: its two counts down a slim strip, and the way back. */
export function ReviewStrip({ comments, decisions, onOpen }: { comments: number; decisions: number; onOpen(): void }) {
  const said = `${comments} open ${comments === 1 ? "comment" : "comments"}, ${decisions} ${decisions === 1 ? "decision" : "decisions"} to take`;
  return (
    <button type="button" aria-label={`Open the review column: ${said}`} aria-expanded={false} title={`Open the review column (${said})`} data-testid="plan-review-strip" onClick={onOpen}
      className="flex w-10 shrink-0 cursor-pointer flex-col items-center gap-3 border-l border-line pt-2.5 text-ink-3 hover:bg-surface-2 hover:text-ink-2 focus-visible:-outline-offset-2">
      <ChevronRightIcon size={14} className="rotate-180" />
      <span className="grid justify-items-center gap-1" data-count="comments">
        <CommentIcon size={14} />
        <span className={`font-mono text-[11px] ${comments ? "text-ink" : ""}`}>{comments}</span>
      </span>
      <span className="grid justify-items-center gap-1" data-count="decisions">
        <i className={`${box} ${decisions ? "border-branch" : "border-pass bg-pass"}`} />
        <span className={`font-mono text-[11px] ${decisions ? "text-ink" : ""}`}>{decisions}</span>
      </span>
    </button>
  );
}
