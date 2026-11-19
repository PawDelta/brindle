/* The Share menu's link (brindle/pro/share_links.py): Copy link makes the design's link, or reuses
   the one it has, and copies it; Revoke link turns it off. Pro and up only: below Pro the daemon says
   `available: false` and the menu shows neither. An org's link policy refusing keeps its own reason. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { DesignShareLink } from "../../../api/client";
import { button, quiet } from "../../../components/ui";
import { message } from "./chrome";
import type { PlanShareLinkClient, ShareLinkClient } from "./client";

/* Where the link is asked for: `key` names the design, so a switch drops the earlier one's answers. */
export interface ShareLinkTarget {
  key: string;
  read(): Promise<DesignShareLink>;
  copy(): Promise<DesignShareLink>;
  revoke(): Promise<DesignShareLink>;
}

/* A Library design, by its doc id (someone who may edit another person's design). */
export function designLinkTarget(client: Partial<ShareLinkClient>, docId: string, repo?: string): ShareLinkTarget | undefined {
  const { designShareLink: read, copyDesignShareLink: copy, revokeDesignShareLink: revoke } = client;
  if (!read || !copy || !revoke) return undefined;
  return { key: `doc:${docId}`, read: () => read.call(client, docId, repo), copy: () => copy.call(client, docId, repo), revoke: () => revoke.call(client, docId, repo) };
}

/* The author's plan, by its id, wherever it was opened: the daemon finds its Library doc. */
export function planLinkTarget(client: Partial<PlanShareLinkClient>, planId: string, repo?: string): ShareLinkTarget | undefined {
  const { planShareLink: read, copyPlanShareLink: copy, revokePlanShareLink: revoke } = client;
  if (!read || !copy || !revoke) return undefined;
  return { key: `plan:${planId}`, read: () => read.call(client, planId, repo), copy: () => copy.call(client, planId, repo), revoke: () => revoke.call(client, planId, repo) };
}

/* Whether the design offers a link at all: null until the daemon answers, and on any failure to read
   it (signed out, offline), so a menu never shows actions it can't carry out. */
export function useShareLink(target: ShareLinkTarget | undefined) {
  const [state, setState] = useState<DesignShareLink | null>(null);
  const latest = useRef(target);
  latest.current = target;
  const key = target?.key;
  useEffect(() => {
    setState(null);
    const t = latest.current;
    if (!t) return;
    let live = true;
    t.read().then((got) => { if (live) setState(got); }, () => {});
    return () => { live = false; };
  }, [key]);
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
  const asked = useRef(target.key);
  asked.current = target.key;
  const run = useCallback(async (call: () => Promise<DesignShareLink>, after?: (got: DesignShareLink) => Promise<void>) => {
    const key = target.key;
    setBusy(true);
    setError(null);
    setCopied(false);
    try {
      const got = await call();
      if (asked.current !== key) return;
      onState(got);
      await after?.(got);
    } catch (e) {
      if (asked.current === key) setError(message(e));
    } finally {
      if (asked.current === key) setBusy(false);
    }
  }, [target.key, onState]);
  const copy = () => run(() => target.copy(), async (got) => {
    if (!got.link) return;
    try {
      await navigator.clipboard.writeText(got.link.url);
      setCopied(true);
    } catch {
      setError("Couldn't copy the link: select it below and copy it yourself.");
    }
  });
  const revoke = () => run(() => target.revoke());
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
