/* The Inbox page: every decision across
   every session, each item a banner card (the kind, one readable line of
   what is asked, its detail, the agent and branch, the session, the age),
   grouped by the session it concerns (org notices at the end), each group
   with "Open the session ›" and each card with "Go to session ›" (the
   item's agent in its session: its provider screen, nothing over it).

   Each card either acts right here or goes to the session, never through
   a popup, a reply field or a moment to undo: one click, sent at once
   (POST /inbox/{id}/{action}), and the card goes; the engine's refusal, if
   any, comes back as one line on it. Permissions are one card
   (PermissionAsks.tsx), whoever holds them: Deny, Allow once, Allow ▾ for a
   request brindle's hook holds, a note and a way to the agent's window for
   a prompt only that window can take. Acted on here too: a lapsed sign-in (Sign in: the provider CLI's own login on the engine, or
   brindle's cloud sign-in for a "login" item), a plan (Approve; changes
   are asked for in its session), proposed checks (Accept / Decline), an
   org notice (Mark read) and a setup problem the engine can repair itself
   (Fix: a job on the engine, POST /v1/setup/codex-hook for a Codex that no
   longer trusts brindle's permission hook; the card goes when the job is
   done, or shows the job's error). Everything else (an agent waiting at
   its own prompt, a failure, a branch to review or merge again) says so in
   one quiet line and only goes to the session. Requests brindle's hook
   holds come grouped, above the rest: one item per kind and general form,
   decided at once for every request in it, under the stall banner.

   Above them all, the app's own item (AppUpdateItem.tsx): an update the
   app installed by itself, with Restart to update, then "Updated to
   brindle X" once. On the whole Inbox, a quiet line (StorageNudge.tsx)
   when brindle could give back over 5 GB of disk, to Settings › Storage.

   The same items count on the sidebar's "Inbox · N" at its top, and
   nowhere else: the Inbox is where what needs a person is read and acted
   on. */

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { DaemonClient, InboxAction } from "../../api/client";
import { useDaemon } from "../../api/context";
import { openExternal, type OpenUrl } from "../../api/open";
import type { AppUpdates } from "../../api/tauri";
import type { InboxItemDTO } from "../../api/schema";
import { PageHeader } from "../../components/PageHeader";
import type { SheetRef } from "../../routes";
import { alert, button, card, quiet } from "../../components/ui";
import { agentName, formatAge, rootOf } from "../../store/overview";
import { useSessions } from "../../store/sessions";
import { useAppState } from "../../store/useStore";
import type { Route } from "../../routes";
import { useJob } from "../Branch/useJob";
import { appUpdateItem } from "../../store/appUpdates";
import { AppUpdateCard } from "./AppUpdateItem";
import { ItemActions } from "./ItemActions";
import { PermissionAsks, PermissionItemCard, usePermissionAsks } from "./PermissionAsks";
import { StorageNudge } from "./StorageNudge";
import { FIXES, KIND_SHORT, REVIEW_KINDS, askKey, describeItem, fixable, forgetGone, itemKey, kindLabel, markShown, opensVisual, pruneShown, seenAt, seenKey, shownKeys, unseenKeys } from "./items";
import { useInboxActions } from "./useInboxActions";

export interface InboxScreenProps {
  client: DaemonClient;
  /* Fetch the snapshot and inbox again (Sync.refresh). */
  refresh: () => Promise<void>;
  /* Open an item's agent in its session: its provider screen, nothing over it unless `sheet` says
     (a prepared PR's item opens Review & open PR). */
  onOpen?(root: string, repo: string, agent?: string, sheet?: SheetRef): void;
  onNavigate?(route: Route): void;
  /* Only what waits in this session (its root agent id), from its bar's count; null for everything. */
  session?: string | null;
  /* Back to everything. */
  onShowAll?(): void;
  /* Settings › Permissions with a rule's editor open (none: the unmatched setting). */
  onOpenRule?(rule?: string): void;
  /* The app's own updates (AppUpdateItem.tsx): the shell's in the app, none in a browser tab. */
  appUpdates?: AppUpdates;
  /* Opens a page in the system browser (an update's What's new). */
  openUrl?: OpenUrl;
}

interface Group {
  key: string;
  title: string;
  subtitle?: string;
  root?: string;
  repo?: string | null;
  items: InboxItemDTO[];
}

/* The card's colour by kind: sap for a request, fail for a lapsed sign-in, branch for a question or a plan. */
const TONE: Record<string, { bar: string; ink: string }> = {
  credentials: { bar: "before:bg-fail", ink: "text-fail" },
  login: { bar: "before:bg-fail", ink: "text-fail" },
  question: { bar: "before:bg-branch", ink: "text-branch" },
  plan: { bar: "before:bg-branch", ink: "text-branch" },
  proposed_checks: { bar: "before:bg-branch", ink: "text-branch" },
  review: { bar: "before:bg-branch", ink: "text-branch" },
  pr_ready: { bar: "before:bg-branch", ink: "text-branch" },
  pr_failed: { bar: "before:bg-fail", ink: "text-fail" },
  pr_opened: { bar: "before:bg-branch", ink: "text-branch" },
  org_notice: { bar: "before:bg-line-strong", ink: "text-ink-3" },
  org_blocked: { bar: "before:bg-line-strong", ink: "text-ink-3" },
  setup: { bar: "before:bg-fail", ink: "text-fail" },
};
const SAP = { bar: "before:bg-sap", ink: "text-sap" };

const ACT = "inline-flex h-6 items-center whitespace-nowrap rounded-md border px-2 text-[13px] font-medium disabled:opacity-50";
const PLAIN = "border-line-strong bg-transparent text-ink hover:bg-surface-2";
const GHOST = "border-transparent text-ink-3 hover:text-ink";
const LINK = "text-[11.5px] text-branch hover:underline";

export function InboxScreen({ client, refresh, onOpen, onNavigate, session = null, onShowAll, onOpenRule, appUpdates, openUrl = openExternal }: InboxScreenProps) {
  const updateStatus = useAppState((s) => s.appUpdate);
  // The app's own item belongs to no session: shown on the whole Inbox only.
  const update = session === null && appUpdates ? appUpdateItem(updateStatus) : null;
  const inbox = useAppState((s) => s.inbox);
  const workspaces = useAppState((s) => s.workspaces);
  const pastWorkers = useAppState((s) => s.pastWorkers);
  const synced = useAppState((s) => s.synced);
  const error = useAppState((s) => s.error);
  const { find, repoOf, hidden, repos } = useSessions();
  const actions = useInboxActions(client, refresh, 0, inbox);
  const asks = usePermissionAsks(client, inbox);

  useEffect(() => forgetGone(inbox), [inbox]);

  const agents = useMemo(() => workspaces.flatMap((ws) => ws.agents ?? []), [workspaces]);

  // On one session: the items of any of its agents (the whole tree), none from elsewhere.
  // A request the permission items hold is theirs, not a card of its own.
  const inSession = (i: InboxItemDTO) => session === null || (i.root_id ?? (i.agent_id ? rootOf(agents, i.agent_id, hidden) : null)) === session;
  const here = useMemo(() => (session === null ? inbox : inbox.filter(inSession)).filter((i) => !actions.hidden.has(itemKey(i)) && !asks.held(i)), [inbox, session, agents, hidden, actions.hidden, asks.held]); // eslint-disable-line react-hooks/exhaustive-deps
  const askGroups = useMemo(() => asks.groups
    .map((g) => ({ ...g, items: g.items.filter((a) => !pastWorkers.includes(a.agent_id) && (session === null || a.root_id === session)) }))
    .filter((g) => g.items.length > 0), [asks.groups, session, pastWorkers]);
  const waiting = here.length + askGroups.length + (update ? 1 : 0);

  /* What this page showed before it opened is not new; the rest is marked "new" until the person leaves,
     and what leaves the inbox meanwhile is forgotten, so it is new again if it returns. Only what this
     view drew is marked shown on leaving (not other sessions' items a filter hid). */
  const before = useRef(shownKeys());
  const drawn = useRef<string[]>([]);
  drawn.current = [...here.map(seenKey), ...askGroups.flatMap((g) => g.items.map(askKey))];
  useEffect(() => () => markShown(drawn.current), []);
  const fresh = useMemo(() => {
    // Before the first snapshot nothing is known to be gone.
    const inboxKeys = synced ? inbox.map(seenKey) : [...before.current].filter((k) => !k.startsWith("ask:"));
    // Held requests not read yet are not "gone".
    const askKeys = asks.loaded ? asks.groups.flatMap((g) => g.items.map(askKey)) : [...before.current].filter((k) => k.startsWith("ask:"));
    return unseenKeys([...inboxKeys, ...askKeys], before.current);
  }, [inbox, synced, asks.groups, asks.loaded]);
  useEffect(() => {
    if (synced) pruneShown(inbox.map(seenKey));
    if (synced && asks.loaded) pruneShown([...inbox.map(seenKey), ...asks.groups.flatMap((g) => g.items.map(askKey))], true);
  }, [inbox, asks.groups, asks.loaded]);
  const sessionTitle = session === null ? null : (find(session)?.name || (find(session)?.goal ?? session));

  /* By session: the root each item's agent hangs under, with the session's
     goal as the title. A setup item (brindle on this computer) and an org
     notice belong to no session: a group each, after the sessions. */
  const groups = useMemo<Group[]>(() => {
    const bySession = new Map<string, Group>();
    for (const item of here) {
      const root = item.root_id ?? (item.agent_id ? rootOf(agents, item.agent_id, hidden) : null);
      const key = root ?? (item.agent_id ? `ws:${item.workspace_id ?? item.agent_id}` : item.kind === "setup" ? "setup" : "org");
      let group = bySession.get(key);
      if (!group) {
        if (item.kind === "setup" && !root) {
          group = { key, title: "Setup", subtitle: "brindle on this computer", items: [] };
        } else if (root) {
          const s = find(root);
          const repo = repoOf(root);
          group = { key, title: s?.name || s?.goal || root, subtitle: `${root}${repo ? ` · ${repos?.find((r) => r.repo_root === repo)?.name ?? repo}` : ""}`, root, repo, items: [] };
        } else if (item.agent_id) {
          group = { key, title: item.workspace_id ?? item.agent_id, subtitle: "not in a session shown here", items: [] };
        } else {
          group = { key, title: "Your org", items: [] };
        }
        bySession.set(key, group);
      }
      group.items.push(item);
    }
    return [...bySession.values()];
  }, [here, agents, hidden, find, repoOf, repos]);

  const sessionOf = (item: InboxItemDTO) => {
    const root = item.root_id ?? (item.agent_id ? rootOf(agents, item.agent_id, hidden) : null);
    const repo = root ? repoOf(root) : null;
    return root && repo ? { root, repo } : null;
  };
  /* A visual decision opens its reader; other items open the agent or the requested sheet. */
  const goTo = (item: InboxItemDTO, sheet?: SheetRef) => {
    const where = sessionOf(item);
    if (!sheet && opensVisual(item) && item.visual_id) {
      sheet = { kind: "visual", id: item.visual_id };
    }
    if (where && onOpen) onOpen(where.root, where.repo, item.agent_id ?? undefined, ...(sheet ? [sheet] : []));
  };

  const withSession = groups.filter((g) => g.root).length;
  return (
    <section className="relative flex min-h-0 flex-1 flex-col gap-s2" aria-label="Inbox">
      <PageHeader
        title="Inbox"
        lede={
          synced && waiting > 0
            ? `${waiting} waiting across ${withSession} session${withSession === 1 ? "" : "s"}${sessionTitle ? ` (only ${sessionTitle})` : ""}. Every card goes to its session; permissions brindle holds are answered here, the rest in the agent's terminal.`
            : sessionTitle
              ? `Nothing waits on you in ${sessionTitle}.`
              : "Nothing waits on you."
        }
      >
        {sessionTitle && (
          <p className="flex items-center gap-s1 text-sm text-ink-2" data-testid="inbox-session">
            <span>
              Only <span className="font-medium text-ink">{sessionTitle}</span>
            </span>
            {onShowAll && (
              <button type="button" className={quiet} onClick={onShowAll}>
                Show every session{inbox.length > here.length ? ` (${inbox.length})` : ""}
              </button>
            )}
          </p>
        )}
      </PageHeader>
      {error && (
        <div role="alert" className={`${alert} flex items-center justify-between gap-s2`}>
          <span>{error}</span>
          <button type="button" className={button} onClick={() => void refresh()}>
            Retry
          </button>
        </div>
      )}
      {!synced && !error && <p className="text-ink-3">Loading…</p>}
      {synced && waiting === 0 && <AllClear />}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-[18px] overflow-y-auto pb-s3" aria-label="Queue">
        {update && appUpdates && (
          <section className="flex flex-col gap-s1" aria-label="This app">
            <header className="flex items-baseline gap-[10px] px-0.5">
              <h2 className="text-[13.5px] font-semibold">This app</h2>
              <span className="text-[11.5px] text-ink-3">brindle on this computer</span>
            </header>
            <AppUpdateCard key={`${update.kind}:${update.version}`} item={update} updates={appUpdates} openUrl={openUrl} />
          </section>
        )}
        {session === null && <StorageNudge client={client} onNavigate={onNavigate} />}
        <PermissionAsks
          client={client}
          groups={askGroups}
          fresh={fresh}
          onOpenRule={onOpenRule}
          onDone={() => {
            void asks.reload();
            void refresh();
          }}
        />
        {groups.map((group) => (
          <section key={group.key} className="flex flex-col gap-s1" aria-label={group.title}>
            <header className="flex items-baseline gap-[10px] px-0.5">
              <h2 className="min-w-0 truncate text-[13.5px] font-semibold">{group.title}</h2>
              {group.subtitle && <span className="truncate text-[11.5px] text-ink-3">{group.subtitle}</span>}
              <span className="flex-1" />
              {group.root && group.repo && onOpen && (
                <button type="button" className={`${LINK} whitespace-nowrap`} onClick={() => onOpen(group.root!, group.repo!)}>
                  Open the session ›
                </button>
              )}
            </header>
            {group.items.map((item) =>
              item.kind === "permission" ? (
                <PermissionItemCard key={itemKey(item)} item={item} isNew={fresh.has(seenKey(item))} actions={actions} who={item.agent_id ? agentName(workspaces, item.agent_id) : "An agent"} onGo={sessionOf(item) && onOpen ? () => goTo(item) : undefined} />
              ) : (
              <Banner key={itemKey(item)} item={item} actions={actions} who={item.agent_id ? agentName(workspaces, item.agent_id) : null} branch={workspaces.find((ws) => ws.id === item.workspace_id)?.branch ?? item.workspace_id ?? null} sessionTitle={group.root ? group.title : null} repo={group.repo ?? repos?.[0]?.repo_root ?? null} isNew={fresh.has(seenKey(item))} onDone={() => void refresh()} onGo={sessionOf(item) && onOpen ? () => goTo(item) : undefined} onReview={sessionOf(item) && onOpen ? () => goTo(item, { kind: "review" }) : undefined} onNavigate={onNavigate} openUrl={openUrl} />
              ),
            )}
          </section>
        ))}
      </div>
    </section>
  );
}

/* One banner card. */
function Banner({ item, actions, who, branch, sessionTitle, repo, isNew, onDone, onGo, onReview, onNavigate, openUrl }: {
  item: InboxItemDTO;
  openUrl?: OpenUrl;
  actions: ReturnType<typeof useInboxActions>;
  who: string | null;
  branch: string | null;
  sessionTitle: string | null;
  repo: string | null;
  /* Not shown on this page before: marked "new" and briefly highlighted. */
  isNew: boolean;
  onDone(): void;
  /* The agent in its session, nothing over it. */
  onGo?(): void;
  /* Its session with Review & open PR over it. */
  onReview?(): void;
  onNavigate?(route: Route): void;
}) {
  const key = itemKey(item);
  const busy = actions.busy.has(key);
  const note = actions.notes[key];
  const tone = TONE[item.kind] ?? SAP;
  const age = formatAge((Date.now() - seenAt(item)) / 1000);

  let body: ReactNode = null;
  if (item.kind === "plan_offer" && item.plan_offer) {
    const offer = item.plan_offer;
    const verified = offer.milestones.filter((m) => m.verified).length;
    body = <div className="space-y-2 break-words" data-testid="card-detail">
      <p><strong>Session: {offer.session_name}</strong></p>
      <p className="whitespace-pre-wrap">{offer.goal}{offer.detail && `\n${offer.detail}`}</p>
      <div>
        <p data-testid="offer-progress">{verified} of {offer.milestones.length} milestones verified</p>
        <ul className="list-disc pl-4">
          {offer.milestones.map((m, index) => <li key={index}>{m.title}{m.verified ? " — verified" : ""}</li>)}
        </ul>
      </div>
      <p data-testid="offer-scale">{offer.workers} {offer.workers === 1 ? "worker" : "workers"} · {offer.repos} {offer.repos === 1 ? "repository" : "repositories"}</p>
      <p><strong>Why brindle is offering: </strong>{offer.reasons.join(" ") || "The work qualified when this offer was made; the plan has since changed."}</p>
      <p>Yes: the supervisor writes a planning doc for you to approve before more workers start. Not now: the work goes ahead as is.</p>
    </div>;
  } else if (item.kind === "proposed_checks" || item.kind === "plan") {
    // Approved or accepted from this card: shown whole, never cut short.
    body = <pre className="whitespace-pre-wrap font-mono text-xs" data-testid="card-detail">{item.detail}</pre>;
  } else if (item.kind === "org_notice") {
    body = item.detail ? <span>from your org's {item.detail}</span> : null;
  } else if (item.kind === "doc") {
    body = <span>{item.detail ?? `Document ${item.doc}`}{item.section && ` · ${item.section}`}</span>;
  } else if (item.kind === "setup") {
    // What the button does, in a line; a fix this app doesn't know: the engine's detail, if any.
    const fix = item.fix ? FIXES[item.fix] : undefined;
    body = fix ? <span>{fix.help}</span> : item.detail ? <span className="line-clamp-3">{item.detail}</span> : null;
  } else if (item.detail && item.kind !== "credentials" && item.kind !== "login") {
    body = <span className="line-clamp-3">{item.detail}</span>;
  } else if (item.reason && item.reason !== describeItem(item)) {
    body = <span>{item.reason}</span>;
  }

  // One click, at once: the card goes; the engine's refusal, if any, comes back as one line.
  const act = (label: string, action: InboxAction, body: Record<string, unknown> = {}, look = PLAIN, title?: string) => (
    <button key={label} type="button" className={`${ACT} ${look}`} disabled={busy} title={title} onClick={() => actions.run(item, action, body)}>
      {label}
    </button>
  );
  let acts: ReactNode;
  if (item.kind === "plan") {
    acts = (
      <>
        {act("Approve", "approve", { feedback: "" })}
        <span className="text-[11px] text-ink-3">to ask for changes, tell it in its session</span>
      </>
    );
  } else if (item.kind === "proposed_checks") {
    acts = (
      <>
        {act("Accept", "accept", { allow_commands: false })}
        {act("Decline", "decline", {}, GHOST)}
      </>
    );
  } else if (item.kind === "plan_offer") {
    acts = <PlanOfferActions item={item} actions={actions} onDone={onDone} onGo={onGo} />;
  } else if (item.kind === "org_notice") {
    acts = act("Mark read", "read", {}, PLAIN);
  } else if (item.kind === "org_blocked") {
    acts = <span className="text-[11px] text-ink-3">set by your organization</span>;
  } else if (fixable(item)) {
    acts = <SetupFix item={item} actions={actions} onDone={onDone} />;
  } else if (item.kind === "setup") {
    acts = <span className="text-[11px] text-ink-3">fixed from a terminal: brindle doctor says how</span>;
  } else if (item.kind === "credentials" || item.kind === "login" || item.kind === "doc" || REVIEW_KINDS.includes(item.kind)) {
    acts = <ItemActions item={item} actions={actions} repo={repo} onDone={onDone} onGo={item.kind === "doc" ? onGo : undefined} onNavigate={onNavigate} compact />;
  } else if (item.kind === "pr_opened" || item.kind === "pr_failed") {
    const url = item.url;
    acts = (
      <>
        {url ? openUrl && <button type="button" className={`${ACT} ${PLAIN}`} onClick={() => { void openUrl(url); actions.run(item, "pr-not-now", {}); }}>
          Open on GitHub
        </button> : item.kind === "pr_failed" && onReview && <button type="button" className={`${ACT} ${PLAIN}`} onClick={onReview}>
          Review &amp; open PR
        </button>}
        {act("Dismiss", "pr-not-now", {}, GHOST)}
      </>
    );
  } else if (item.kind === "pr_ready") {
    // One press opens it (the sheet's open-PR call, no diffs); the person's press is still the only push.
    acts = (
      <>
        <OpenPr item={item} actions={actions} onDone={onDone} openUrl={openUrl} />
        {act("Dismiss", "pr-not-now", {}, GHOST)}
      </>
    );
  } else if (opensVisual(item)) {
    // A visual decision opens its reader (goTo), not the session it belongs to; disabled until that session has loaded.
    acts = (
      <button type="button" className={`${button} text-xs`} disabled={!onGo} onClick={onGo}>
        Open visual
      </button>
    );
  } else {
    acts = <span className="text-[11px] text-ink-3">{item.kind === "review" || item.kind === "merge_failed" ? "handled in its session" : "answered in its session"}</span>;
  }

  return (
    <article className={`${card} relative grid max-w-[960px] grid-cols-[minmax(0,1fr)_auto] items-start gap-x-s2 gap-y-1 py-[11px] pl-[18px] pr-[14px] before:absolute before:bottom-[10px] before:left-0 before:top-[10px] before:w-[2px] before:rounded-sm before:opacity-80 ${tone.bar} ${isNew ? "inbox-new" : ""}`} aria-label={kindLabel(item)} data-testid="inbox-card" data-kind={item.kind} data-new={isNew || undefined}>
      <div className={`col-start-1 flex items-center gap-1.5 font-mono text-[10.5px] uppercase tracking-[0.06em] ${tone.ink}`}>
        {KIND_SHORT[item.kind] ?? item.kind}
        {isNew && <span className="rounded-full bg-sap px-1.5 text-[10px] normal-case tracking-normal font-semibold leading-4 text-on-sap" data-testid="new-mark">new</span>}
      </div>
      <div className="col-start-1 text-[13.5px] font-semibold leading-[1.35]">{describeItem(item)}</div>
      {body && <div className="col-start-1 min-w-0 text-[12.5px] leading-[1.45] text-ink-2">{body}</div>}
      <div className="col-start-2 row-span-3 row-start-1 flex flex-col items-end gap-0.5 whitespace-nowrap text-right text-[11.5px] text-ink-3">
        {who && (
          <span className="font-mono text-ink-2">
            {who}
            {branch && branch !== who && ` · ${branch}`}
          </span>
        )}
        {sessionTitle && <span className="max-w-64 truncate">{sessionTitle}</span>}
        <span className="font-mono text-[11px]">{age === "just now" ? age : `${age} ago`}</span>
      </div>
      {note && (
        <p className={`col-span-2 text-xs ${note.failed ? "text-sap" : "text-pass"}`} role={note.failed ? "alert" : "status"}>
          {note.text}
        </p>
      )}
      <div className="col-span-2 mt-1 flex flex-wrap items-center gap-1.5">
        {acts}
        <span className="flex-1" />
        {onGo && item.kind !== "doc" && !REVIEW_KINDS.includes(item.kind) && !opensVisual(item) && (
          <button type="button" className={`${button} text-xs whitespace-nowrap`} onClick={onGo}>
            Go to session ›
          </button>
        )}
      </div>
    </article>
  );
}

/* brindle's one-time offer to plan a big goal first: Yes / Not now, answered through the goal
   route with the offer id the item carries (request_id). A refusal (the offer was replaced
   meanwhile) is the one line on the card; either way the inbox is read again. */
function PlanOfferActions({ item, actions, onDone, onGo }: { item: InboxItemDTO; actions: ReturnType<typeof useInboxActions>; onDone(): void; onGo?(): void }) {
  const client = useDaemon();
  const [saving, setSaving] = useState(false);
  const pending = useRef(false);
  const answer = (accept: boolean) => {
    // Guard synchronously: both choices can fire before React disables them.
    if (pending.current) return;
    pending.current = true;
    setSaving(true);
    actions.dismiss(itemKey(item));
    void client
      .resolvePlanOffer(item.root_id ?? "", item.request_id ?? "", accept)
      .then(() => {
        actions.settle(item);
        onGo?.();
      }, (e: unknown) => actions.report(item, e instanceof Error ? e.message : String(e), true))
      .finally(() => {
        pending.current = false;
        setSaving(false);
        onDone();
      });
  };
  return (
    <>
      <button type="button" className={`${ACT} ${PLAIN}`} disabled={saving} onClick={() => answer(true)}>
        Yes
      </button>
      <button type="button" className={`${ACT} ${GHOST}`} disabled={saving} onClick={() => answer(false)}>
        Not now
      </button>
    </>
  );
}

/* A setup item's one button (items.ts FIXES): one click starts the fix on
   the engine, a job followed on the bus. Done, the card goes (and the inbox
   is read again: the engine no longer lists the problem); failed, the job's
   error is the one line on the card, and the button is there to press again.
   No confirmation, no progress card, no popup: the button waits while the
   job runs, as any card's does while its request is out. */
function SetupFix({ item, actions, onDone }: { item: InboxItemDTO; actions: ReturnType<typeof useInboxActions>; onDone(): void }) {
  const client = useDaemon();
  const job = useJob();
  const fix = FIXES[item.fix ?? ""];
  const status = job.job?.status;
  const error = job.job?.error;
  useEffect(() => {
    if (status === "failed") actions.report(item, error ?? "the fix failed", true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, error]);
  const start = () =>
    void job.start(fix.label, () => client[fix.route](), () => {
      actions.settle(item);
      onDone();
    });
  return (
    <button type="button" className={`${ACT} ${PLAIN}`} disabled={job.busy} onClick={start} data-testid="setup-fix">
      {fix.label}
    </button>
  );
}

/* Open PR on a prepared PR's card: the review sheet's own call (POST /sessions/{root}/pr, a job) with the
   draft's title and description, without its diffs. The button waits while the job runs; a refusal shows on the card. */
function OpenPr({ item, actions, onDone, openUrl }: { item: InboxItemDTO; actions: ReturnType<typeof useInboxActions>; onDone(): void; openUrl?: OpenUrl }) {
  const client = useDaemon();
  const job = useJob();
  const [drafting, setDrafting] = useState(false);
  /* gh couldn't open it: the compare page, where the person finishes by hand (no PR exists yet). */
  const [compare, setCompare] = useState<string | null>(null);
  const status = job.job?.status;
  const error = job.job?.error;
  useEffect(() => {
    if (status === "failed") actions.report(item, error ?? "couldn't open the PR", true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, error]);
  const root = item.root_id ?? item.agent_id ?? "";
  const open = async () => {
    setDrafting(true);
    try {
      const draft = await client.prDraft(root);
      await job.start("Opening the PR", () => client.openSessionPr(root, { title: item.detail?.trim() || draft.title, body: draft.body }), (result) => {
        const url = (result as { url?: string } | undefined)?.url;
        if (url && !/\/pull\/\d+/.test(url)) {
          // No PR was created: nothing settles; the compare page is the way on.
          setCompare(url);
          actions.report(item, "GitHub's CLI couldn't open the PR; open it from the compare page.", true);
          return;
        }
        actions.settle(item);
        onDone();
      });
    } catch (e) {
      actions.report(item, e instanceof Error ? e.message : String(e), true);
    } finally {
      setDrafting(false);
    }
  };
  const busy = drafting || job.busy;
  return (
    <>
      <button type="button" className={`${ACT} ${PLAIN}`} disabled={busy} onClick={() => void open()}>
        {busy ? "Opening…" : "Open PR"}
      </button>
      {compare && openUrl && (
        <button type="button" className={`${ACT} ${PLAIN}`} onClick={() => void openUrl(compare)}>
          Open on GitHub
        </button>
      )}
    </>
  );
}

function AllClear() {
  return (
    <div className={`${card} flex flex-col items-center gap-s1 px-s3 py-s6 text-center`} data-testid="all-clear">
      <span className="flex size-10 items-center justify-center rounded-full bg-pass-soft text-lg text-pass" aria-hidden>
        ✓
      </span>
      <p className="text-base font-semibold">All clear</p>
      <p className="max-w-sm text-sm text-ink-2">Nothing needs you. The workers keep going, and anything that waits on you shows up here and on the sidebar's Inbox entry.</p>
    </div>
  );
}
