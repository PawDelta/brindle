/* The design doc's status under its title: where it stands, what sign-off waits for,
   who wrote it and when it last changed, and the brief behind a link. Reviewers live in Share. */

import { pill } from "../../../components/ui";
import type { WorkItem } from "./DocHistory";

const sep = <span aria-hidden className="h-3.5 w-px bg-line-strong" />;

export function DocStatus({ stage, tone, line, byline, workItem, brief, briefOpen, onBrief, testid = "plan-status", state, held, presentation }: {
  /* "In team review", "Locked for sign-off", "Needs your sign-off"… */
  stage: string;
  tone: "sap" | "pass" | "branch";
  /* What sign-off waits for, in words. */
  line: string;
  /* "by Maya Chen and her supervisor · changed 5h ago" */
  byline: string;
  workItem?: WorkItem | null;
  brief?: boolean;
  briefOpen?: boolean;
  onBrief?(): void;
  testid?: string;
  state?: string;
  held?: string;
  /* The presentation the stage was chosen for ("closed": planning closed without approval). */
  presentation?: string;
}) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-3.5 gap-y-1.5 [overflow-wrap:anywhere] font-sans text-[12.5px] text-ink-2" data-testid={testid} data-state={state} data-held={held} data-presentation={presentation}>
      <span className={`${pill} ${tone === "pass" ? "text-pass" : tone === "branch" ? "text-branch" : "text-sap"}`}>{stage}</span>
      <span>{line}</span>
      {sep}
      {workItem && (
        <span className="inline-flex min-w-0 flex-wrap items-center gap-1.5 rounded-sm border border-line px-1.5 font-mono text-[11px] leading-[17px] text-ink-2" title={workItem.title}>
          <i className={`size-1.5 rounded-full ${workItem.done ? "bg-pass" : workItem.active ? "bg-[var(--group-1)]" : "bg-ink-3"}`} />
          {`${workItem.key} · ${workItem.state}`}
        </span>
      )}
      <span className="text-ink-3">{byline}</span>
      {brief && <button type="button" className="-ml-1 cursor-pointer rounded-md px-2 py-0.5 text-xs text-ink-2 hover:bg-surface-2 hover:text-ink" aria-expanded={briefOpen} onClick={onBrief}>{briefOpen ? "The brief ▾" : "The brief ▸"}</button>}
    </div>
  );
}
