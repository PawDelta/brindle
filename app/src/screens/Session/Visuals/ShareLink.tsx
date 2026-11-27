/* The Share menu's link (brindle/pro/share_links.py): Copy link makes the design's link, or reuses
   the one it has, and copies it; Revoke link turns it off. Pro and up only: below Pro the daemon says
   `available: false` and the menu shows neither. An org's link policy refusing keeps its own reason. */

import { useCallback, useEffect, useRef, useState } from "react";
import { DaemonError, type DesignShareLink, type DocScope, type ShareLinkScope } from "../../../api/client";
import { ShareChevron } from "./SharePanel";
import { message } from "./chrome";
import { LibraryNetworkNote, SlowConnectionNote, libraryNetwork, useSlowPending, type LibraryNetwork } from "./LibraryNetwork";
import type { PlanShareLinkClient, ShareLinkClient } from "./client";

/* Where the link is asked for: `key` names the design, so a switch drops the earlier one's answers. */
export interface ShareLinkTarget {
  key: string;
  read(): Promise<DesignShareLink>;
  copy(scope?: ShareLinkScope): Promise<DesignShareLink>;
  update?(settings: ShareLinkScope): Promise<DesignShareLink>;
  scopes?(): Promise<{ scopes: DocScope[] }>;
  revoke(): Promise<DesignShareLink>;
}

/* A Library design, by its doc id (someone who may edit another person's design). */
export function designLinkTarget(client: Partial<ShareLinkClient>, docId: string, repo?: string): ShareLinkTarget | undefined {
  const { designShareLink: read, copyDesignShareLink: copy, revokeDesignShareLink: revoke } = client;
  if (!read || !copy || !revoke) return undefined;
  return { key: `doc:${docId}:${repo ?? ""}`, read: () => read.call(client, docId, repo), copy: (scope) => copy.call(client, docId, repo, scope), revoke: () => revoke.call(client, docId, repo),
    update: client.updateDesignShareLink ? (settings) => client.updateDesignShareLink!(docId, settings, repo) : undefined,
    scopes: client.docScopes ? () => client.docScopes!(repo) : undefined };
}

/* The author's plan, by its id, wherever it was opened: the daemon finds its Library doc. */
export function planLinkTarget(client: Partial<PlanShareLinkClient>, planId: string, repo?: string): ShareLinkTarget | undefined {
  const { planShareLink: read, copyPlanShareLink: copy, revokePlanShareLink: revoke } = client;
  if (!read || !copy || !revoke) return undefined;
  return { key: `plan:${planId}:${repo ?? ""}`, read: () => read.call(client, planId, repo), copy: (scope) => copy.call(client, planId, repo, scope), revoke: () => revoke.call(client, planId, repo),
    update: client.updatePlanShareLink ? (settings) => client.updatePlanShareLink!(planId, settings, repo) : undefined,
    scopes: client.docScopes ? () => client.docScopes!(repo) : undefined };
}

/* Read the link when the Share menu opens, when the plan is shared, or when the person tries again
   after a busy sign-in. Only the newest answer counts: a copy or revoke the person made (`setState`)
   outdates every read still out, which saw the link before it. */
export function useShareLink(target: ShareLinkTarget | undefined, fresh?: unknown) {
  const [state, setRead] = useState<DesignShareLink | null>(null);
  const [blocked, setBlocked] = useState<{ reason: string; reading: boolean; network?: LibraryNetwork } | null>(null);
  const [reads, setReads] = useState(0);
  const latest = useRef(target);
  latest.current = target;
  const generation = useRef(0);
  const key = target?.key;
  useEffect(() => {
    generation.current++;
    setRead(null);
    setBlocked(null);
  }, [key]);
  useEffect(() => {
    const t = latest.current;
    if (!t) return;
    const asked = ++generation.current;
    setBlocked((current) => current && { ...current, reading: true });
    t.read().then((got) => {
      if (generation.current !== asked) return;
      setRead(got);
      setBlocked(null);
    }, (e: unknown) => {
      if (generation.current !== asked) return;
      const network = libraryNetwork(e);
      if (e instanceof DaemonError && (e.code === "busy" || network)) {
        setBlocked({ reason: e.message, reading: false, network: network ?? undefined });
      } else setBlocked(null);
    });
    return () => { if (generation.current === asked) generation.current++; };
  }, [key, fresh, reads]);
  const setState = useCallback((next: DesignShareLink) => {
    generation.current++;
    setRead(next);
    setBlocked(null);
  }, []);
  return { state, setState, blocked, readAgain: () => setReads((n) => n + 1) };
}

export function ShareLinkSection({ target, link }: { target: ShareLinkTarget; link: ReturnType<typeof useShareLink> }) {
  return <>
    {link.blocked && <section className="sec" aria-label="Plan link">
      {link.blocked.network
        ? <LibraryNetworkNote state={link.blocked.network} onTryAgain={link.blocked.reading ? undefined : link.readAgain} />
        : <div className="msg bad" role="alert"><span className="grow">{link.blocked.reason}</span>
          <button type="button" className="sp-btn q end" disabled={link.blocked.reading} onClick={link.readAgain}>Try again</button></div>}
    </section>}
    {link.state?.available && <ShareLinkPanel target={target} state={link.state} onState={link.setState} />}
  </>;
}

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
  const [permission, setPermission] = useState<"view" | "edit">("view");
  const audience = state.link?.scope?.kind === "team" ? `team:${state.link.scope.team}` : "org";
  // A changed authoritative audience invalidates the old choice. An unchanged
  // refresh must leave a choice the person is still making intact.
  useEffect(() => { setSelection(audience); }, [target.key, state.scoped, audience]);
  useEffect(() => { setPermission(state.link?.permission ?? "view"); }, [target.key, state.link?.permission]);
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
  useEffect(() => {
    asked.current = target.key;
    return () => { asked.current = ""; };
  }, [target.key]);
  const inFlight = useRef<string | null>(null);
  /* The change a network failure stopped, for Try again. */
  const [net, setNet] = useState<LibraryNetwork | null>(null);
  const last = useRef<{ call: () => Promise<DesignShareLink>; after?: (got: DesignShareLink) => Promise<void> } | null>(null);
  const run = useCallback(async (call: () => Promise<DesignShareLink>, after?: (got: DesignShareLink) => Promise<void>) => {
    const key = target.key;
    if (inFlight.current === key) return;
    inFlight.current = key;
    last.current = { call, after };
    setBusy(true);
    setError(null);
    setNet(null);
    setCopied(false);
    try {
      const got = await call();
      if (asked.current !== key) return;
      onState(got);
      await after?.(got);
    } catch (e) {
      if (asked.current === key) { setError(message(e)); setNet(libraryNetwork(e)); }
    } finally {
      if (inFlight.current === key) inFlight.current = null;
      if (asked.current === key) setBusy(false);
    }
  }, [target.key, onState]);
  const org = scopes?.find((scope) => scope.kind === "org");
  const teams = scopes?.filter((scope) => scope.kind === "group") ?? [];
  const valid = !state.scoped || (selection === "org" ? !!org : teams.some((team) => `team:${team.id}` === selection));
  const settings = (audience = selection, access = permission): ShareLinkScope => ({ permission: access, ...(state.scoped
    ? audience === "org" ? { scope: "org" as const } : { scope: "team" as const, team: audience.slice(5) }
    : {}) });
  const change = (audience: string, access: "view" | "edit") => {
    if (inFlight.current === target.key) return;
    setCopied(false);
    setError(null);
    if (state.link && target.update) void run(() => target.update!(settings(audience, access)));
    else { setSelection(audience); setPermission(access); }
  };
  const copy = () => run(() => target.copy(settings()), async (got) => {
    if (!got.link) return;
    try {
      await navigator.clipboard.writeText(got.link.url);
      if (asked.current === target.key) setCopied(true);
    } catch {
      if (asked.current === target.key) setError("Couldn't copy the link: select it below and copy it yourself.");
    }
  });
  const revoke = () => run(async () => {
    const got = await target.revoke();
    return { ...state, ...got, access: state.access ?? state.link?.access };
  });
  const slow = useSlowPending(busy);
  const link = state.link;
  const locked =state.access === "off" || link?.access === "off";
  return (
    <section className="sec" data-testid="share-link" data-state={busy ? "saving" : error ? "error" : "ready"}>
      <div className="lab"><b>Plan link</b>{(link || locked) && <button type="button" className="sp-btn q end" aria-label="Revoke link" disabled={busy} onClick={revoke}>Revoke</button>}</div>
      {locked ? <p className="hint" style={{ marginTop: 2 }}>{org?.name ?? "Your organization"} has turned plan links off, so no one can open this plan by link.</p> : <>
        <div className="line">
          {state.scoped ? <label className="sel"><select aria-label="Link audience" value={selection} disabled={busy || !scopes} className="min-w-0 appearance-none bg-transparent outline-none"
            onChange={(e) => change(e.target.value, permission)}>
            <option value="org" disabled={!org}>Anyone in {org?.name ?? org?.id ?? "your organization"}</option>
            {teams.map((team) => <option key={team.id} value={`team:${team.id}`}>Only {team.name ?? team.id}</option>)}
          </select><ShareChevron /></label> : <span className="sel">{state.access === "org" || link?.access === "org" ? "Anyone in your organization" : "Anyone with the link"}</span>}
          <label className="sel"><select aria-label="Link access" data-testid="share-link-access" className="appearance-none bg-transparent outline-none" value={permission} disabled={busy}
            onChange={(e) => change(selection, e.target.value as "view" | "edit")}><option value="view">can view</option><option value="edit">can edit</option></select><ShareChevron /></label>
        </div>
        <div className="line">{link ? <input className="url" readOnly aria-label="Share link" value={link.url} onFocus={(e) => e.currentTarget.select()} />
          : <span className="grow text-xs text-ink-3">Copy link to create a link to this plan.</span>}
          <button type="button" className="sp-btn s" disabled={busy || !valid} onClick={copy}>{copied ? "Copied" : "Copy link"}</button>
        </div>
        {link && <p className="hint">People with access can open this plan in their browser. Everyone else sees a page saying it couldn't be found.</p>}
      </>}
      {slow && <SlowConnectionNote />}
      {error && net && <LibraryNetworkNote state={net} onTryAgain={() => { if (last.current) void run(last.current.call, last.current.after); }} />}
      {error && !net && <p role="alert" className="hint bad">{error}</p>}
    </section>
  );
}
