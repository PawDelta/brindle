/* A plan opened from its share link (a brindle:// link, lib/deeplink.ts), in the doc screen
   (PlanReader.tsx). The daemon reads it through the link as the account signed in
   (brindle/pro/shared_reader.py read_link): the link's own checks decide, as for its web page, so
   nothing opens here that the browser wouldn't. Read only: the person reads it, as on the web.
   Nothing is kept on this machine, and the token never leaves this screen's memory.

   A link that didn't parse, or one the link's checks refuse, says so calmly. Signed out (or the
   sign-in expired), the screen asks the person to sign in and opens the plan once they have. */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { DaemonError, type SharedDesign } from "../../../api/client";
import { button } from "../../../components/ui";
import { CloudLoginStatus, useCloudLogin } from "../../Inbox/CloudSignIn";
import type { PlanLink } from "../../../lib/deeplink";
import { BackButton, message, useEscBack } from "./chrome";
import type { ShareLinkDesignClient } from "./client";
import { linkSource } from "./docSource";
import { ReaderBar } from "./DocBar";
import { DesignDoc, MODES, useDoc, type Mode, type Teammate } from "./PlanReader";

/* How often a plan open here is read again, so losing access takes it off the screen. */
const READ_EVERY_MS = 30_000;
const VIEWING = MODES.filter((m) => m.id === "viewing");
const NOT_A_LINK = "Couldn't open this link. It isn't a link to a brindle plan.";

type First = { kind: "reading" } | { kind: "open"; id: string } | { kind: "signed-out" } | { kind: "failed"; error: string };

function why(e: unknown): First {
  if (e instanceof DaemonError && e.status === 402) return { kind: "signed-out" };
  return { kind: "failed", error: `Couldn't open this link. ${message(e)}` };
}

export function SharedLinkReader({ client, link, onBack, backLabel }: {
  client: ShareLinkDesignClient;
  link: PlanLink | null;
  onBack(): void;
  backLabel?: string;
}) {
  useEscBack(onBack);
  const [first, setFirst] = useState<First>({ kind: "reading" });
  const [asked, setAsked] = useState(0);
  const token = link?.token ?? null;
  // The first read finds the plan's id here (the daemon's, never the token).
  useEffect(() => {
    if (!token) return;
    let live = true;
    setFirst({ kind: "reading" });
    client.shareLinkDesign(token).then(
      (doc) => { if (live) setFirst({ kind: "open", id: doc.id }); },
      (e: unknown) => { if (live) setFirst(why(e)); },
    );
    return () => { live = false; };
  }, [client, token, asked]);
  const lead = (title?: string) => (
    <>
      <BackButton onBack={onBack} label={backLabel} />
      <span className="mx-1 h-5 w-px bg-line" aria-hidden />
      <h2 className="min-w-0 truncate text-[13px] font-semibold">{title ?? "Shared plan"}</h2>
    </>
  );
  // A sign-in lost while the plan is open: ask for it again, keeping the link.
  const signedOut = useCallback(() => setFirst({ kind: "signed-out" }), []);
  if (token && first.kind === "open") return <LinkDoc client={client} token={token} id={first.id} lead={lead} signedOut={signedOut} />;
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col" data-testid="shared-link-reader">
      <ReaderBar lead={lead()} />
      <div className="p-s3 text-sm">
        {!token ? (
          <p className="text-ink-2" role="status" data-testid="deep-link-invalid">{NOT_A_LINK}</p>
        ) : first.kind === "reading" ? (
          <p className="text-ink-3">Loading…</p>
        ) : first.kind === "signed-out" ? (
          <SignInFirst onSignedIn={() => setAsked((n) => n + 1)} />
        ) : first.kind === "failed" ? (
          <div className="flex flex-col items-start gap-s2" role="status" data-testid="deep-link-failed">
            <p className="text-ink-2">{first.error}</p>
            <button type="button" className={button} onClick={() => setAsked((n) => n + 1)}>Try again</button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

/* Signed out: brindle's own sign-in in the browser, then the plan. */
function SignInFirst({ onSignedIn }: { onSignedIn(): void }) {
  const login = useCloudLogin();
  const done = login.job?.status === "done";
  const then = useRef(onSignedIn);
  then.current = onSignedIn;
  // Once per sign-in: the plan is read again, and this goes once it opens.
  useEffect(() => {
    if (done) then.current();
  }, [done]);
  return (
    <div className="flex flex-col items-start gap-s2" data-testid="deep-link-sign-in">
      <p className="text-ink-2">Sign in to brindle to open this plan.</p>
      <button type="button" className={button} onClick={login.start} disabled={login.busy}>Sign in</button>
      <CloudLoginStatus job={login.job} />
    </div>
  );
}

function LinkDoc({ client, token, id, lead, signedOut }: {
  client: ShareLinkDesignClient;
  token: string;
  id: string;
  lead(title?: string): ReactNode;
  signedOut(): void;
}) {
  const source = useMemo(() => linkSource(client, token, id), [client, token, id]);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setTick((n) => n + 1), READ_EVERY_MS);
    return () => clearInterval(timer);
  }, []);
  // Only a read that still counts (useDoc's say) can take the plan back to Sign in: a late 402 from
  // a read a newer one has passed, or from before the last sign-in, can't.
  const { doc, error, writes } = useDoc<SharedDesign>(source, tick, (e) => {
    if (e instanceof DaemonError && e.status === 402) signedOut();
  });
  const viewing = (m: Mode) => m === "viewing";
  const teammate: Teammate | undefined = doc ? {
    me: doc.me,
    rights: { comment: false, suggest: false, signOff: false },
    signedOff: false,
    author: null,
    directory: [],
  } : undefined;
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col" data-testid="shared-link-reader">
      {doc && teammate ? (
        <DesignDoc key={doc.id} source={source} teammate={teammate} plan={doc} mode="viewing" writes={writes}
          chrome={{ lead: lead(doc.title), modes: VIEWING, allowed: viewing, onMode: () => {} }} />
      ) : (
        <>
          <ReaderBar lead={lead()} />
          <p className="p-s3 text-sm text-ink-3" role={error ? "status" : undefined}>{error ? `Couldn't open this link. ${error}` : "Loading…"}</p>
        </>
      )}
    </div>
  );
}
