/* The design doc's status, in one line under its title (PlanReader.tsx): where it stands, what
   sign-off waits for, the reviewers as faces (with a tick or a ring once a version is frozen),
   who wrote it and when it last changed, and the brief behind a link. It replaces a form of
   Status / Author / Reviewers / Understood rows. */

import type { ReactNode } from "react";
import { pill } from "../../../components/ui";
import type { WorkItem } from "./DocHistory";
import { Avatar } from "./PlanCards";

export interface StatusReviewer {
  key: string;
  who: string;
  /* First name, or "You". */
  name: string;
  required: boolean;
  signed: boolean;
}

const sep = <span aria-hidden className="h-3.5 w-px bg-line-strong" />;

export function DocStatus({ stage, tone, line, reviewers, frozen, byline, workItem, brief, briefOpen, onBrief, testid = "plan-status", state, held }: {
  /* "In team review", "Locked for sign-off", "Needs your sign-off"… */
  stage: string;
  tone: "sap" | "pass" | "branch";
  /* What sign-off waits for, in words. */
  line: string;
  reviewers: StatusReviewer[];
  /* A version is frozen: each reviewer shows by name with where they stand. */
  frozen: boolean;
  /* "by Maya Chen and her supervisor · changed 5h ago" */
  byline: string;
  workItem?: WorkItem | null;
  brief?: boolean;
  briefOpen?: boolean;
  onBrief?(): void;
  testid?: string;
  state?: string;
  held?: string;
}) {
  const faces: ReactNode = frozen ? reviewers.map((r) => (
    <span key={r.key} className={`inline-flex items-center gap-1.5 ${r.required ? "" : "text-ink-3"}`} title={r.required ? (r.signed ? `${r.name} signed` : `${r.name}'s sign-off is required`) : "Optional reviewer"}>
      <Avatar who={r.who} ring={r.signed ? "done" : r.required ? "wait" : "dim"} />
      {r.name}
      {r.required ? (r.signed ? <span className="text-pass">✓</span> : <span className="text-sap">waiting</span>) : <span>· optional</span>}
    </span>
  )) : reviewers.length > 0 && (
    <span className="inline-flex items-center gap-1.5" title={`Required: ${reviewers.filter((r) => r.required).map((r) => r.name).join(", ") || "nobody"}${reviewers.some((r) => !r.required) ? `. Optional: ${reviewers.filter((r) => !r.required).map((r) => r.name).join(", ")}` : ""}`}>
      <span className="text-ink-3">Reviewers</span>
      <span className="inline-flex">
        {reviewers.map((r, i) => <span key={r.key} className={`inline-flex rounded-full ${i ? "-ml-[5px] shadow-[0_0_0_1.5px_var(--canvas)]" : ""}`}><Avatar who={r.who} ring={r.required ? undefined : "dim"} /></span>)}
      </span>
    </span>
  );
  return (
    <div className="flex flex-wrap items-center gap-x-3.5 gap-y-1.5 font-sans text-[12.5px] text-ink-2" data-testid={testid} data-state={state} data-held={held}>
      <span className={`${pill} ${tone === "pass" ? "text-pass" : tone === "branch" ? "text-branch" : "text-sap"}`}>{stage}</span>
      <span>{line}</span>
      {(frozen || reviewers.length > 0) && sep}
      {faces}
      {sep}
      {workItem && (
        <span className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-sm border border-line px-1.5 font-mono text-[11px] leading-[17px] text-ink-2" title={workItem.title}>
          <i className={`size-1.5 rounded-full ${workItem.done ? "bg-pass" : workItem.active ? "bg-[var(--group-1)]" : "bg-ink-3"}`} />
          {`${workItem.key} · ${workItem.state}`}
        </span>
      )}
      <span className="text-ink-3">{byline}</span>
      {brief && <button type="button" className="-ml-1 cursor-pointer rounded-md px-2 py-0.5 text-xs text-ink-2 hover:bg-surface-2 hover:text-ink" aria-expanded={briefOpen} onClick={onBrief}>{briefOpen ? "The brief ▾" : "The brief ▸"}</button>}
    </div>
  );
}
