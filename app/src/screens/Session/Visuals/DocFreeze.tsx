/* The strip under a shared design doc's toolbar (DocPresence.tsx draws it from the live channel's
   state). Three kinds, one at a time:
   - Live: who is in the doc, that changes save as they are typed, and Freeze with what it does.
     The author freezes; a teammate is told who can.
   - Locked for sign-off: the one place that says a version is frozen, by whom, who wrote it, and
     that comments stay open. The author can Reopen it (which withdraws its sign-offs).
   - The author went offline: everyone else's text is locked until they are back. */

import type { ReactNode } from "react";
import type { LiveDocState, LiveRefusal } from "../../../api/client";
import { button, main, quiet } from "../../../components/ui";

const strip = "flex flex-wrap items-center gap-x-2.5 gap-y-1 border-b border-line px-s2 py-[7px] font-sans text-[12.5px] text-ink";

export const LockIcon = ({ className = "" }: { className?: string }) => (
  <svg viewBox="0 0 16 16" aria-hidden className={`size-3.5 flex-none fill-none stroke-current stroke-[1.5] ${className}`} strokeLinecap="round" strokeLinejoin="round">
    <rect x="3.5" y="7" width="9" height="6.5" rx="1" /><path d="M5.5 7V5a2.5 2.5 0 015 0v2" />
  </svg>
);

/* "Maya", "Maya and Dana", "Maya, Dana and you". */
export function names(list: string[]): string {
  if (list.length <= 1) return list[0] ?? "";
  return `${list.slice(0, -1).join(", ")} and ${list.at(-1)}`;
}

function Refused({ refused }: { refused: LiveRefusal | null }) {
  if (!refused) return null;
  return (
    <p role="alert" data-state={`${refused.action}-refused`} className="m-0 basis-full text-sap">
      {refused.action === "freeze" ? "Not frozen" : "Not reopened"}{refused.reason ? `: ${refused.reason}` : ""}
    </p>
  );
}

export function LiveStrip({ isAuthor, author, here, reviewers, editing, connected, refused, onFreeze }: {
  isAuthor: boolean;
  /* The author's name, as the live channel has it. */
  author: string;
  /* Everyone in the doc now, this person as "You" first. */
  here: string[];
  /* Who is asked to sign once it is frozen. */
  reviewers: string[];
  /* The person is in Editing mode (else the author reads, with Freeze at hand). */
  editing: boolean;
  connected: boolean;
  refused: LiveRefusal | null;
  onFreeze(): void;
}) {
  const others = here.length - 1;
  const who = editing
    ? `${names(here)} ${here.length === 1 ? "is" : "are"} in this doc. Changes save as you type.`
    : `${others > 0 ? `${names(here)} are in this doc.` : "Nobody else is in this doc."}`;
  const asks = reviewers.length ? ` and asks ${names(reviewers)} to sign it` : "";
  return (
    <section aria-label="Live document" data-state="editing" data-testid="doc-live-strip" className={`${strip} bg-[color-mix(in_srgb,var(--group-1)_9%,transparent)]`}>
      <span aria-hidden className="size-[7px] flex-none rounded-full bg-[var(--group-1)] shadow-[0_0_0_3px_color-mix(in_srgb,var(--group-1)_25%,transparent)]" />
      <b className="font-semibold">Live</b>
      <span className="min-w-0 text-ink-2">{who}</span>
      <span className="flex-1" />
      {isAuthor ? (
        <>
          <span className="text-ink-3">{`Freeze locks the text as a version${asks}.`}</span>
          <button type="button" className={`${main} text-xs`} disabled={!connected} onClick={onFreeze}>Freeze for sign-off</button>
        </>
      ) : (
        <span role="status" data-state="author-required" className="text-ink-3">{`Only ${author || "the author"} can freeze it for sign-off`}</span>
      )}
      <Refused refused={refused} />
    </section>
  );
}

export function LockStrip({ version, author, writers, isAuthor, connected, refused, onReopen, onHistory }: {
  version: NonNullable<LiveDocState["version"]>;
  author: string;
  writers: string[];
  isAuthor: boolean;
  connected: boolean;
  refused: LiveRefusal | null;
  onReopen(): void;
  onHistory?(): void;
}) {
  return (
    <section aria-label="Live document" data-state="sign_off" data-testid="doc-lock-strip" className={`${strip} bg-branch-soft`}>
      <LockIcon className="text-branch" />
      <b className="font-semibold" data-testid="doc-version" data-version={version.id}>{`Version ${version.id} is locked for sign-off`}</b>
      <span className="text-ink-2">
        {`Frozen by ${author || "the author"} · written by `}
        <ul aria-label="Credited writers" className="m-0 inline list-none p-0">
          {writers.map((w, i) => <li key={w} className="inline">{`${w}${i < writers.length - 2 ? ", " : i === writers.length - 2 ? " and " : ""}`}</li>)}
        </ul>
        {" · comments still open"}
      </span>
      <span className="flex-1" />
      {onHistory && <button type="button" className={`${quiet} text-xs`} onClick={onHistory}>See what changed</button>}
      {isAuthor && <button type="button" className={`${button} text-xs`} disabled={!connected} title="Unlock the text for editing; sign-offs on this version are withdrawn" onClick={onReopen}>Reopen</button>}
      <Refused refused={refused} />
    </section>
  );
}

export function OfflineStrip({ author, children }: { author: string; children?: ReactNode }) {
  return (
    <section aria-label="Live document" data-state="author_offline" className={`${strip} bg-sap-soft`}>
      <p role="status" className="m-0 text-ink">{`${author || "The author"} went offline while editing · comments and suggestions only`}</p>
      {children}
    </section>
  );
}
