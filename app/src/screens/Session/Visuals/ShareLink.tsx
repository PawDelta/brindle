/* The Share menu's link (brindle/pro/share_links.py): Copy link makes the design's link, or reuses
   the one it has, and copies it; Revoke link turns it off. Pro and up only: below Pro the daemon says
   `available: false` and the menu shows neither. An org's link policy refusing keeps its own reason. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { DesignShareLink, DocScope, ShareLinkScope } from "../../../api/client";
import { button, quiet } from "../../../components/ui";
import { message } from "./chrome";
import type { PlanShareLinkClient, ShareLinkClient } from "./client";

/* Where the link is asked for: `key` names the design, so a switch drops the earlier one's answers. */
export interface ShareLinkTarget {
  key: string;
  read(): Promise<DesignShareLink>;
  copy(scope?: ShareLinkScope): Promise<DesignShareLink>;
  scopes?(): Promise<{ scopes: DocScope[] }>;
  revoke(): Promise<DesignShareLink>;
}

/* A Library design, by its doc id (someone who may edit another person's design). */
export function designLinkTarget(client: Partial<ShareLinkClient>, docId: string, repo?: string): ShareLinkTarget | undefined {
  const { designShareLink: read, copyDesignShareLink: copy, revokeDesignShareLink: revoke } = client;
  if (!read || !copy || !revoke) return undefined;
  return { key: `doc:${docId}:${repo ?? ""}`, read: () => read.call(client, docId, repo), copy: (scope) => copy.call(client, docId, repo, scope), revoke: () => revoke.call(client, docId, repo),
    scopes: client.docScopes ? () => client.docScopes!(repo) : undefined };
}

/* The author's plan, by its id, wherever it was opened: the daemon finds its Library doc. */
export function planLinkTarget(client: Partial<PlanShareLinkClient>, planId: string, repo?: string): ShareLinkTarget | undefined {
  const { planShareLink: read, copyPlanShareLink: copy, revokePlanShareLink: revoke } = client;
  if (!read || !copy || !revoke) return undefined;
  return { key: `plan:${planId}:${repo ?? ""}`, read: () => read.call(client, planId, repo), copy: (scope) => copy.call(client, planId, repo, scope), revoke: () => revoke.call(client, planId, repo),
    scopes: client.docScopes ? () => client.docScopes!(repo) : undefined };
}

/* Whether the design offers a link at all: null until the daemon answers, and on any failure to read
   it (signed out, offline), so a menu never shows actions it can't carry out. Read again whenever
   `fresh` changes (the Share menu opening, a plan getting shared into the Library): a plan with no
   link a moment ago may have one now. Only the newest answer counts: a copy or revoke the person
   made (`setState`) outdates every read still out, which saw the link before it. */
export function useShareLink(target: ShareLinkTarget | undefined, fresh?: unknown) {
  const [state, setRead] = useState<DesignShareLink | null>(null);
  const latest = useRef(target);
  latest.current = target;
  const generation = useRef(0);
  const key = target?.key;
  useEffect(() => {
    generation.current++;
    setRead(null);
  }, [key]);
  useEffect(() => {
    const t = latest.current;
    if (!t) return;
    const asked = ++generation.current;
    t.read().then((got) => { if (generation.current === asked) setRead(got); }, () => {});
  }, [key, fresh]);
  const setState = useCallback((next: DesignShareLink) => {
    generation.current++;
    setRead(next);
  }, []);
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
  const [scopes, setScopes] = useState<DocScope[] | null>(null);
  const [selection, setSelection] = useState("org");
  const audience = state.link?.scope?.kind === "team" ? `team:${state.link.scope.team}` : "org";
  // A changed authoritative audience invalidates the old choice. An unchanged
  // refresh must leave a choice the person is still making intact.
  useEffect(() => { setSelection(audience); }, [target.key, state.scoped, audience]);
  const latest = useRef(target);
  latest.current = target;
  useEffect(() => {
    let active = true;
    setScopes(null);
    setError(null);
    setCopied(false);
    setBusy(false);
    if (state.scoped) latest.current.scopes?.().then(
      (got) => { if (active) setScopes(got.scopes); },
      (e) => { if (active) setError(message(e)); },
    );
    return () => { active = false; };
  }, [target.key, state.scoped]);
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
  const org = scopes?.find((scope) => scope.kind === "org");
  const teams = scopes?.filter((scope) => scope.kind === "group") ?? [];
  const valid = !state.scoped || (selection === "org" ? !!org : teams.some((team) => `team:${team.id}` === selection));
  const copy = () => run(() => target.copy(state.scoped
    ? selection === "org" ? { scope: "org" } : { scope: "team", team: selection.slice(5) }
    : undefined), async (got) => {
    if (!got.link) return;
    try {
      await navigator.clipboard.writeText(got.link.url);
      setCopied(true);
    } catch {
      setError("Couldn't copy the link: select it below and copy it yourself.");
    }
  });
  const revoke = () => run(async () => {
    const got = await target.revoke();
    return { ...state, ...got, access: state.access ?? state.link?.access };
  });
  const link = state.link;
  const locked = state.access === "off" || link?.access === "off";
  return (
    <section className="grid gap-1.5 border-t border-line pt-2.5" data-testid="share-link">
      <div className="flex items-center gap-2">
        <span className="text-[12.5px] font-semibold text-ink">Link</span>
        <span className="flex-1" />
        {!locked && <button type="button" className={`${button} text-xs`} disabled={busy || !valid} onClick={copy}>{copied ? "Copied" : "Copy link"}</button>}
        {(link || locked) && <button type="button" className={`${quiet} text-xs`} disabled={busy} onClick={revoke}>Revoke link</button>}
      </div>
      {state.scoped && !locked && <select aria-label="Link audience" value={selection} disabled={busy || !scopes}
        className="rounded border border-line bg-canvas px-2 py-1 text-xs text-ink"
        onChange={(e) => { setSelection(e.target.value); setCopied(false); setError(null); }}>
        <option value="org" disabled={!org}>Anyone in {org?.name ?? org?.id ?? "your organization"}</option>
        {teams.map((team) => <option key={team.id} value={`team:${team.id}`}>Only {team.name ?? team.id}</option>)}
      </select>}
      {link && !locked && <input readOnly aria-label="Share link" value={link.url} onFocus={(e) => e.currentTarget.select()}
        className="w-full rounded border border-line bg-canvas px-2 py-1 font-mono text-[11.5px] text-ink-2" />}
      <p className="m-0 text-[12px] leading-[1.5] text-ink-3">
        {locked ? state.reason ?? ACCESS.off : link?.scope?.kind === "team"
          ? "Only signed-in members of this team or its subteams can open it."
          : link?.scope?.kind === "org" ? ACCESS.org
          : link ? ACCESS[link.access] : state.access ? ACCESS[state.access] : "Anyone with the link can read it, and comment once signed in."}
        {!locked && state.reason && <> {state.reason}</>}
        {" "}Revoking a link stops it opening, but can't take back copies people already opened.
      </p>
      {error && <p role="alert" className="m-0 text-[12px] text-fail">{error}</p>}
    </section>
  );
}
