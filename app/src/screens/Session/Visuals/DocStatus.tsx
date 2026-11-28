/* The design doc's status under its title: where it stands, what sign-off waits for,
   who wrote it and when it last changed, and the brief behind a link. Reviewers live in Share. */

import { pill } from "../../../components/ui";
import { DocReference } from "../../../components/DocReference";

export function DocStatus({ stage, tone, line, byline, reference, brief, briefOpen, onBrief, testid = "plan-status", state, held, presentation }: {
  /* "In team review", "Locked for sign-off", "Needs your sign-off"… */
  stage: string;
  tone: "sap" | "pass" | "branch";
  /* What sign-off waits for, in words. */
  line: string;
  /* The revision or version and when it last changed. */
  byline: string;
  reference?: string | null;
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
      <span className={`${pill} shrink-0 ${tone === "pass" ? "text-pass" : tone === "branch" ? "text-branch" : "text-sap"}`}>{stage}</span>
      <span>{line}</span>
      <span className="inline-flex max-w-full items-center gap-x-3.5 text-ink-3">
        <span aria-hidden className="h-3.5 w-px shrink-0 bg-line-strong" />
        <span>{byline}</span>
        <DocReference reference={reference} />
      </span>
      {brief && <button type="button" className="-ml-1 cursor-pointer rounded-md px-2 py-0.5 text-xs text-ink-2 hover:bg-surface-2 hover:text-ink" aria-expanded={briefOpen} onClick={onBrief}>{briefOpen ? "The brief ▾" : "The brief ▸"}</button>}
    </div>
  );
}
