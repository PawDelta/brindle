/* A design someone else shared, opened from the Inbox's "Review requested" card: the doc screen
   (PlanReader.tsx) on the Library's copy. Read, comment, suggest,
   vote and sign off, as far as the Library lets this account; never approve or build. Editing is
   offered only where the doc's text is live and the Library lets this account write: the live
   text, never a revision of its own. Nothing is kept on this machine: the doc lives in this
   component's state and is read from the Library again while open, so losing access takes it off
   the screen. */

import { useEffect, useMemo, useState } from "react";
import type { LibraryPerson, SharedDesign } from "../../../api/client";
import type { SharedDesignClient } from "./client";
import type { LiveClient } from "./DocEditor";
import { BackButton, useEscBack } from "./chrome";
import { sharedSource } from "./docSource";
import { ReaderBar } from "./DocBar";
import { DesignDoc, MODES, useDoc, type Mode, type Teammate } from "./PlanReader";

/* How often a design open here is read again: what teammates wrote, and whether it can still be opened. */
const READ_EVERY_MS = 30_000;

const COLUMN_MODES = MODES.filter((m) => m.id !== "editing");

/* Who wrote and voted, by name where the org's directory knows them. */
function named(doc: SharedDesign, directory: LibraryPerson[]): SharedDesign {
  const name = (who: string) => directory.find((p) => p.sub === who)?.email || who;
  return {
    ...doc,
    decisions: doc.decisions.map((d) => ({ ...d, votes: d.votes && Object.fromEntries(Object.entries(d.votes).map(([option, subs]) => [option, subs.map(name)])) })),
    threads: doc.threads?.map((t) => ({ ...t, author: name(t.author), replies: t.replies?.map((r) => ({ ...r, author: name(r.author) })) })),
  };
}

export function SharedDesignReader({ client, docId, title, repo, onBack, backLabel }: {
  client: SharedDesignClient & Partial<LiveClient>;
  docId: string;
  /* The card's title, while the first read is out. */
  title?: string;
  repo?: string;
  onBack(): void;
  /* Where Back goes, in words ("Library"). */
  backLabel?: string;
}) {
  useEscBack(onBack);
  const source = useMemo(() => sharedSource(client, docId, repo), [client, docId, repo]);
  const [tick, setTick] = useState(0);
  const [mode, setMode] = useState<Mode>("viewing");
  const [directory, setDirectory] = useState<LibraryPerson[]>([]);
  useEffect(() => {
    const timer = setInterval(() => setTick((n) => n + 1), READ_EVERY_MS);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    let live = true;
    client.libraryPeople().then((got) => { if (live) setDirectory(got.people); }, () => {});
    return () => { live = false; };
  }, [client]);
  const { doc, error, writes } = useDoc(source, tick);
  const shown = useMemo(() => doc && { ...named(doc, directory), title: title ?? doc.title }, [doc, directory, title]);
  const allowed = (m: Mode) => m === "viewing" || (!!shown?.can_suggest && m === "suggesting") || (m === "editing" && !!shown?.can_edit && !!client.visualLive);
  const current = allowed(mode) ? mode : "viewing";
  const teammate: Teammate | undefined = shown ? {
    me: shown.me,
    rights: { comment: shown.can_comment ?? false, suggest: shown.can_suggest ?? false, signOff: shown.can_sign_off ?? false },
    signedOff: shown.signed_off ?? false,
    author: shown.author ?? null,
    directory,
  } : undefined;
  const lead = (
    <>
      <BackButton onBack={onBack} label={backLabel} />
      <span className="mx-1 h-5 w-px bg-line" aria-hidden />
      <h2 className="min-w-0 truncate text-[13px] font-semibold">{title ?? shown?.title ?? "Design doc"}</h2>
    </>
  );
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col" data-testid="shared-design-reader">
      {shown && teammate ? (
        <DesignDoc key={shown.id} source={source} teammate={teammate} plan={shown} mode={current} writes={writes} liveClient={client.visualLive ? client as LiveClient : undefined}
          chrome={{ lead, modes: client.visualLive ? MODES : COLUMN_MODES, allowed, onMode: setMode }} />
      ) : (
        <>
          <ReaderBar lead={lead} />
          <p className="p-s3 text-sm text-ink-3" role={error ? "alert" : undefined}>{error ?? "Loading…"}</p>
        </>
      )}
    </div>
  );
}
