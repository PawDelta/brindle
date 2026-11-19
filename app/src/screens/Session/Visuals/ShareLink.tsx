/* The Share menu's link (brindle/pro/share_links.py): Copy link makes the design's link, or reuses
   the one it has, and copies it; Revoke link turns it off. Pro and up only: below Pro the daemon says
   `available: false` and the menu shows neither. An org's link policy refusing keeps its own reason. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { DesignShareLink } from "../../../api/client";
import { button, quiet } from "../../../components/ui";
import { message } from "./chrome";
import type { ShareLinkClient } from "./client";

export interface ShareLinkTarget {
  client: ShareLinkClient;
  docId: string;
  repo?: string;
}

/* Whether the design offers a link at all: null until the daemon answers, and on any failure to read
   it (signed out, offline), so a menu never shows actions it can't carry out. */
export function useShareLink(target: ShareLinkTarget | undefined) {
  const [state, setState] = useState<DesignShareLink | null>(null);
  const client = target?.client;
  const docId = target?.docId;
  const repo = target?.repo;
  useEffect(() => {
    setState(null);
    if (!client || !docId) return;
    let live = true;
    client.designShareLink(docId, repo).then((got) => { if (live) setState(got); }, () => {});
    return () => { live = false; };
  }, [client, docId, repo]);
  return { state, setState };
}

const ACCESS: Record<"anyone" | "org" | "off", string> = {
  anyone: "Anyone with the link can read it, and comment once signed in.",
  org: "Only signed-in members of your organization can open it.",
  off: "Your organization has turned share links off, so it opens for no one.",
};

export function ShareLinkPanel({ target, state, onState }: {
  target: ShareLinkTarget;
  state: DesignShareLink;
  onState(next: DesignShareLink): void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  // Whose answer this is: a design switched under an open menu drops the earlier one's.
  const asked = useRef(target.docId);
  asked.current = target.docId;
  const run = useCallback(async (call: () => Promise<DesignShareLink>, after?: (got: DesignShareLink) => Promise<void>) => {
    const doc = target.docId;
    setBusy(true);
    setError(null);
    setCopied(false);
    try {
      const got = await call();
      if (asked.current !== doc) return;
      onState(got);
      await after?.(got);
    } catch (e) {
      if (asked.current === doc) setError(message(e));
    } finally {
      if (asked.current === doc) setBusy(false);
    }
  }, [target.docId, onState]);
  const copy = () => run(() => target.client.copyDesignShareLink(target.docId, target.repo), async (got) => {
    if (!got.link) return;
    try {
      await navigator.clipboard.writeText(got.link.url);
      setCopied(true);
    } catch {
      setError("Couldn't copy the link: select it below and copy it yourself.");
    }
  });
  const revoke = () => run(() => target.client.revokeDesignShareLink(target.docId, target.repo));
  const link = state.link;
  return (
    <section className="grid gap-1.5 border-t border-line pt-2.5" data-testid="share-link">
      <div className="flex items-center gap-2">
        <span className="text-[12.5px] font-semibold text-ink">Link</span>
        <span className="flex-1" />
        <button type="button" className={`${button} text-xs`} disabled={busy} onClick={copy}>{copied ? "Copied" : "Copy link"}</button>
        {link && <button type="button" className={`${quiet} text-xs`} disabled={busy} onClick={revoke}>Revoke link</button>}
      </div>
      {link && <input readOnly aria-label="Share link" value={link.url} onFocus={(e) => e.currentTarget.select()}
        className="w-full rounded border border-line bg-canvas px-2 py-1 font-mono text-[11.5px] text-ink-2" />}
      <p className="m-0 text-[12px] leading-[1.5] text-ink-3">
        {link ? ACCESS[link.access] : "A link lets people outside this design's team read it."}
        {" "}Revoking a link stops it opening, but can't take back copies people already opened.
      </p>
      {error && <p role="alert" className="m-0 text-[12px] text-fail">{error}</p>}
    </section>
  );
}
