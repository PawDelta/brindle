/* The design doc's status under its title: where it stands, what approval waits for,
   who wrote it and when it last changed, and the brief behind a link. Reviewers live in Share. */

import { pill } from "../../../components/ui";
import { DocReference } from "../../../components/DocReference";

export function DocStatus({ stage, tone, line, byline, reference, brief, briefOpen, onBrief, testid = "plan-status", state, held, presentation }: {
  /* "In team review", "Locked for approval", "Needs your approval"… */
  stage: string;
  tone: "sap" | "pass" | "branch";
  line?: string;
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
    <div className="min-w-0 [overflow-wrap:anywhere] font-sans text-[12.5px] text-ink-2" data-testid={testid} data-state={state} data-held={held} data-presentation={presentation}>
      <span className={`${pill} whitespace-nowrap ${tone === "pass" ? "text-pass" : tone === "branch" ? "text-branch" : "text-sap"}`}>{stage}</span>{" "}
      {line && <><span className="border-r border-line-strong px-2">{line}</span>{" "}</>}
      <span className="text-ink-3">{byline}</span>
      {reference && <>{" "}<DocReference reference={reference} className="ml-1" /></>}
      {brief && <>{" "}<button type="button" className="cursor-pointer rounded-md px-2 py-0.5 text-xs text-ink-2 hover:bg-surface-2 hover:text-ink" aria-expanded={briefOpen} onClick={onBrief}>{briefOpen ? "The brief ▾" : "The brief ▸"}</button></>}
    </div>
  );
}
