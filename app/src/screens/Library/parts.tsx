/* The pieces a list row and a board card share: the stage pill, the linked work item, the
   reviewers' faces with their sign-off rings, and how the scope a design lives in is named. */

import type { DocScope, WorkItemLink } from "../../api/client";
import { pill } from "../../components/ui";
import type { DesignRecord, DesignStage } from "./libraryStages";
import { signOffs, taskProgress } from "./libraryStages";

/* A design's scope, as the list's "Shared with" column names it. */
export function scopeLabel(scope?: DocScope, named?: DocScope[]): string {
  if (!scope || scope.kind === "user") return "Only me";
  const known = named?.find((s) => s.kind === scope.kind && s.id === scope.id);
  return known?.name || scope.name || (scope.kind === "org" ? "Organization" : `Team ${scope.id.slice(0, 8)}`);
}

/* What the pill says: the person's part when the design waits on them, else its stage (Building
   with how many of its plan's tasks merged). */
export function StagePill({ stage, need, record }: { stage: DesignStage; need: "review" | "changes" | null; record?: DesignRecord | null }) {
  if (need) return <span className={`${pill} whitespace-nowrap text-sap`} data-stage="needs-you">{need === "review" ? "Your review" : "Changes asked"}</span>;
  const progress = taskProgress(record);
  switch (stage) {
    case "building":
      return <span className={`${pill} whitespace-nowrap text-pass`} data-stage={stage}>Building{progress ? ` · ${progress.merged} of ${progress.total} green` : ""}</span>;
    case "approved":
      return <span className={`${pill} text-pass`} data-stage={stage}>Approved</span>;
    case "shipped":
      return <span className={`${pill} text-ink-2`} data-stage={stage}>Shipped</span>;
    case "in-review":
      return <span className={`${pill} pill-hollow text-ink-2`} data-stage={stage}>In review</span>;
    default:
      return <span className={`${pill} pill-hollow text-ink-3`} data-stage={stage}>Drafting</span>;
  }
}

/* The linked work item's key and state; nothing until the design has one. */
export function WorkItem({ item }: { item?: WorkItemLink | null }) {
  if (!item?.key) return null;
  const dot = item.tone === "done" ? "bg-pass" : item.tone === "active" ? "bg-[var(--group-1)]" : "bg-ink-3";
  return (
    <span className="inline-flex items-center gap-[5px] whitespace-nowrap rounded-[3px] border border-line px-1.5 font-mono text-[11px] leading-[17px] text-ink-2" title={item.title ?? undefined} data-work-item={item.key}>
      <i className={`h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} />
      {item.state ? `${item.key} · ${item.state}` : item.key}
    </span>
  );
}

const TONES = ["--group-1", "--group-2", "--group-4", "--group-7", "--group-8", "--group-6", "--group-5", "--group-9"];
const tone = (sub: string) => TONES[[...sub].reduce((n, c) => (n * 31 + c.charCodeAt(0)) >>> 0, 7) % TONES.length];
/* Two letters for a person: the first of each part of their name or address. */
export function initials(sub: string): string {
  const name = sub.split("@")[0].replace(/^u_/, "");
  const parts = name.split(/[._\-\s]+/).filter(Boolean);
  return (parts.length > 1 ? parts[0][0] + parts[1][0] : name.slice(0, 2)).toUpperCase();
}

export function Face({ sub, ring }: { sub: string; ring?: "done" | "wait" }) {
  const shadow = ring === "done" ? "0 0 0 1.5px var(--canvas), 0 0 0 3px var(--pass)" : ring === "wait" ? "0 0 0 1.5px var(--canvas), 0 0 0 3px var(--sap)" : "0 0 0 1.5px var(--canvas)";
  return (
    <span className="inline-grid h-4 w-4 shrink-0 place-items-center rounded-full font-mono text-[7.5px] font-semibold leading-none text-canvas" style={{ background: `var(${tone(sub)})`, boxShadow: shadow }} title={sub}>
      {initials(sub)}
    </span>
  );
}

/* The reviewers' faces (a ring once they signed the revision on screen) and how many required
   sign-offs are in; "—" for a design nobody reviews. */
export function SignOffs({ record, say }: { record?: DesignRecord | null; say?: string }) {
  const s = signOffs(record?.design);
  if (!s) return <span className="text-xs text-ink-3">—</span>;
  return (
    <span className="inline-flex items-center gap-2 text-xs text-ink-3" data-signed={`${s.signed}/${s.required}`}>
      <span className="inline-flex [&>*+*]:-ml-[5px]">{s.people.map((p) => <Face key={p.sub} sub={p.sub} ring={p.signed ? "done" : undefined} />)}</span>
      {say ? <span>{say}</span> : <span className="font-mono text-ink-2">{s.signed}/{s.required}</span>}
    </span>
  );
}
