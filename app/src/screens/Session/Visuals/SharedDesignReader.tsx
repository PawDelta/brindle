/* A design someone else shared, opened from the Inbox's "Review requested" card: the doc screen
   (PlanReader.tsx) on the Library's copy. Read, comment, suggest,
   vote and sign off, as far as the Library lets this account; never approve or build. Editing is
   offered only where the doc's text is live and the Library lets this account write: the live
   text, never a revision of its own. The document lives in memory and access is checked again
   while open. The daemon holds pending review changes for this account. */

import { useEffect, useMemo, useRef, useState } from "react";
import type { LibraryPerson, PlanDeliveryItem, SharedDesign } from "../../../api/client";
import type { ShareLinkClient, SharedDesignClient } from "./client";
import { designLinkTarget } from "./ShareLink";
import type { LiveClient } from "./DocEditor";
import { BackButton, useEscBack } from "./chrome";
import { sharedSource } from "./docSource";
import { ReaderBar } from "./DocBar";
import { DesignDoc, MODES, useDoc, type Mode, type Teammate } from "./PlanReader";
import type { Work } from "./WorkPanel";
import { displayName } from "./PlanCards";
import { AccessRequest } from "./AccessRequest";
import { PlanDeliveryProvider, QueueCount } from "./PlanDelivery";

/* How often a design open here is read again: what teammates wrote, and whether it can still be opened. */
const READ_EVERY_MS = 30_000;

function observeVisibleReads(read: () => void) {
  let timer: ReturnType<typeof setInterval> | undefined;
  const update = (event?: Event) => {
    clearInterval(timer);
    timer = undefined;
    if (document.visibilityState === "hidden") return;
    // Recheck permissions and review changes as soon as the reader returns to view.
    if (event) read();
    timer = setInterval(read, READ_EVERY_MS);
  };
  update();
  document.addEventListener("visibilitychange", update);
  window.addEventListener("online", update);
  return () => {
    clearInterval(timer);
    document.removeEventListener("visibilitychange", update);
    window.removeEventListener("online", update);
  };
}

const COLUMN_MODES = MODES.filter((m) => m.id !== "editing");

/* Who wrote and voted, by name where the org's directory knows them. */
function named(doc: SharedDesign, directory: LibraryPerson[]): SharedDesign {
  const names = { ...doc.teammate_names };
  for (const person of directory) {
    const saved = names[person.sub];
    names[person.sub] = displayName({ ...person, email: null }) || (saved !== "A teammate" && saved) || person.email || "A teammate";
  }
  const name = (who: string, label?: string | null) => who === "person" ? null
    : (names[who] !== "A teammate" && names[who]) || (label !== "A teammate" && label) || "A teammate";
  return {
    ...doc,
    signed_off: doc.signed_off || doc.outbox?.some(item => item.state === "waiting" && item.tail === "/sign-offs" && item.body.rev === doc.rev),
    decisions: doc.decisions.map(d => {
      const pick = doc.outbox?.filter(item => item.tail === "/votes" && item.body.decision === d.id).at(-1);
      if (!pick || pick.state === "sent") return d;
      return { ...d, confirmed: pick.state === "refused" ? null : pick.body.option };
    }),
    teammate_names: names,
    threads: doc.threads?.map((t) => ({ ...t, author_name: name(t.author, t.author_name), replies: t.replies?.map((r) => ({ ...r, author_name: name(r.author, r.author_name) })) })),
  };
}

export function SharedDesignReader({ client, docId, title, repo, onBack, backLabel, work }: {
  client: SharedDesignClient & Partial<LiveClient> & Partial<ShareLinkClient>;
  /* The design's work items, when it was opened from the Library. */
  work?: Work;
  docId: string;
  /* The card's title, while the first read is out. */
  title?: string;
  repo?: string;
  onBack(): void;
  /* Where Back goes, in words ("Library"). */
  backLabel?: string;
}) {
  useEscBack(onBack);
  const account = useRef<string | undefined>(undefined);
  const [sending, setSending] = useState<{ docId: string; me?: string; item: PlanDeliveryItem } | null>(null);
  const source = useMemo(() => sharedSource(client, docId, repo, item => {
    const pending = item && { docId, me: account.current, item };
    setSending(previous => pending ?? (previous?.docId === docId ? null : previous));
  }), [client, docId, repo]);
  const [mode, setMode] = useState<Mode>("viewing");
  const [directory, setDirectory] = useState<LibraryPerson[]>([]);
  useEffect(() => {
    let live = true;
    client.libraryPeople().then((got) => { if (live) setDirectory(got.people); }, () => {});
    return () => { live = false; };
  }, [client]);
  const { doc, error, writes } = useDoc(source, undefined, undefined, observeVisibleReads);
  account.current = doc?.me;
  const shown = useMemo(() => {
    if (!doc) return null;
    const current = sending?.docId === doc.id && sending.me === doc.me
      ? { ...doc, outbox: [...(doc.outbox ?? []), sending.item] } : doc;
    return { ...named(current, directory), title: title ?? doc.title };
  }, [doc, directory, title, sending]);
  const allowed = (m: Mode) => m === "viewing" || (!!shown?.can_suggest && m === "suggesting") || (m === "editing" && !!shown?.can_edit && !!client.visualLive);
  const current = allowed(mode) ? mode : "viewing";
  const teammate: Teammate | undefined = shown ? {
    me: shown.me,
    rights: { comment: shown.can_comment ?? false, suggest: shown.can_suggest ?? false, signOff: shown.can_sign_off ?? false },
    signOff: shown.sign_off,
    signOffs: shown.sign_offs,
    signedOff: shown.signed_off ?? false,
    author: shown.author ?? null,
    directory,
  } : undefined;
  const lead = (
    <>
      <BackButton onBack={onBack} label={backLabel} />
      <QueueCount items={doc?.outbox} />
      <span className="mx-1 h-5 w-px bg-line" aria-hidden />
      <h2 title={title ?? shown?.title} className="min-w-0 truncate text-[13px] font-semibold">{title ?? shown?.title ?? "Plan"}</h2>
    </>
  );
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col" data-testid="shared-design-reader">
      {doc && shown && teammate ? (
        <PlanDeliveryProvider key={shown.id} items={shown.outbox ?? []}><DesignDoc source={source} teammate={teammate} plan={shown} mode={current} writes={writes} work={work}
          top={<AccessRequest key={docId} client={client} doc={doc} repo={repo} />}
          shareLink={shown.can_edit ? designLinkTarget(client, docId, repo) : undefined}
          liveClient={client.visualLive ? client as LiveClient : undefined}
          chrome={{ lead, modes: (client.visualLive ? MODES : COLUMN_MODES).map((m) => m.id === "viewing" && !shown.can_comment ? { ...m, hint: "Read this plan" } : m), allowed, onMode: setMode }} /></PlanDeliveryProvider>
      ) : (
        <>
          <ReaderBar lead={lead} />
          <p className="p-s3 text-sm text-ink-3" role={error ? "alert" : undefined}>{error ?? "Loading…"}</p>
        </>
      )}
    </div>
  );
}
