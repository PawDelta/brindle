/* The Inbox page (design/options-v3/C2b-inbox-only): every decision across
   every session, each item a banner card (the kind, one readable line of
   what is asked, its detail, the agent and branch, the session, the age),
   grouped by the session it concerns (org notices at the end), each group
   with "Open the session ›" and each card with "Go to session ›" (the
   item's agent in its session: its provider screen, nothing over it).

   Each card either acts right here or goes to the session, never through
   a popup, a reply field or a moment to undo: one click, sent at once
   (POST /inbox/{id}/{action}), and the card goes; the engine's refusal, if
   any, comes back as one line on it. Acted on here: a permission request
   brindle's hook holds (the provider's own choices as small buttons), a
   lapsed sign-in (Sign in: the provider CLI's own login on the engine, or
   brindle's cloud sign-in for a "login" item), a plan (Approve; changes
   are asked for in its session), proposed checks (Accept / Decline), an
   org notice (Mark read) and a setup problem the engine can repair itself
   (Fix: a job on the engine, POST /v1/setup/codex-hook for a Codex that no
   longer trusts brindle's permission hook; the card goes when the job is
   done, or shows the job's error). Everything else (an agent waiting at
   its own prompt, a permission only that prompt can take, a failure, a
   branch to review or merge again) says so in one quiet line and only
   goes to the session.

   The same items count on the sidebar's "Inbox · N" at its top, and
   nowhere else: the Inbox is where what needs a person is read and acted
   on. */

import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { DaemonClient, InboxAction } from "../../api/client";
import { useDaemon } from "../../api/context";
import type { InboxItemDTO } from "../../api/schema";
import { PageHeader } from "../../components/PageHeader";
import { alert, button, card, quiet } from "../../components/ui";
import { agentName, formatAge, rootOf } from "../../store/overview";
import { useSessions } from "../../store/sessions";
import { useAppState } from "../../store/useStore";
import { useJob } from "../Branch/useJob";
import { ItemActions } from "./ItemActions";
import { FIXES, KIND_SHORT, answerable, describeItem, fixable, forgetGone, itemKey, kindLabel, permissionAnswer, permissionLine, seenAt } from "./items";
import { useInboxActions } from "./useInboxActions";

export interface InboxScreenProps {
  client: DaemonClient;
  /* Fetch the snapshot and inbox again (Sync.refresh). */
  refresh: () => Promise<void>;
  /* Open an item's agent in its session: its provider screen, nothing over it. */
  onOpen?(root: string, repo: string, agent?: string): void;
  /* Only what waits in this session (its root agent id), from its bar's count; null for everything. */
  session?: string | null;
  /* Back to everything. */
  onShowAll?(): void;
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
  org_notice: { bar: "before:bg-line-strong", ink: "text-ink-3" },
  setup: { bar: "before:bg-fail", ink: "text-fail" },
};
const SAP = { bar: "before:bg-sap", ink: "text-sap" };

const ACT = "inline-flex h-6 items-center whitespace-nowrap rounded-md border px-2 text-[11.5px] font-medium disabled:opacity-50";
const PRIMARY = "border-sap bg-sap text-on-sap hover:opacity-90";
const PLAIN = "border-line-strong bg-surface text-ink hover:bg-surface-2";
const GHOST = "border-transparent text-ink-3 hover:text-ink";
const LINK = "text-[11.5px] text-branch hover:underline";

function PermissionActions({ item, actions, busy }: {
  item: InboxItemDTO;
  actions: ReturnType<typeof useInboxActions>;
  busy: boolean;
}) {
  const p = item.permission!;
  const cap = p.max_minutes;
  const [duration, setDuration] = useState(String(Math.min(30, cap ?? 30)));
  const [unit, setUnit] = useState("minutes");
  const [scope, setScope] = useState("branch");
  const adapter = p.via === "grant";
  const scopes = adapter ? p.approval_scopes ?? {} : {};
  const selectedScope = scope in scopes ? scope : Object.keys(scopes)[0];
  const savedScope = adapter ? (selectedScope ? scopes[selectedScope] : p.approval_scope) : null;
  const timedScope = adapter ? savedScope : p.approval_scope;
  const multiplier = unit === "days" ? 1440 : unit === "hours" ? 60 : 1;
  const minutes = Number(duration) * multiplier;
  const valid = Number.isSafeInteger(minutes) && minutes > 0 && cap != null && minutes <= cap;
  const timed = p.choices?.some((c) => c.choice === "allow_for") && cap != null;
  const expires = valid ? new Date(Date.now() + minutes * 60000) : null;
  const order = ["deny", "allow_once", "allow_session", "allow_for", "allow_always"];
  const savedChoices = ["allow_session", "allow_for", "allow_always"];
  const choices = [...(p.choices ?? [])].sort((a, b) => order.indexOf(a.choice) - order.indexOf(b.choice));
  const permanent = choices.find((c) => c.choice === "allow_always");
  return (
    <div className="flex min-w-0 flex-col gap-2">
      {Object.keys(scopes).length > 1 && (
        <label className="flex flex-wrap items-center gap-2 text-xs text-ink-3">
          Saved approval scope
          <select aria-label="Saved approval scope" className="rounded border border-line-strong bg-surface px-2 py-1 text-ink"
            disabled={busy} value={selectedScope} onChange={(e) => setScope(e.target.value)}>
            {Object.keys(scopes).map((level) => <option key={level} value={level}>
              {({ branch: "Requested branch on this remote", remote: "All branches on this remote",
                repo: "All permitted remotes in this repository",
                all: "Across repositories (all permitted remotes and branches)",
                repository: "All permitted remotes in this repository" } as Record<string, string>)[level] ?? level}
            </option>)}
          </select>
        </label>
      )}
      {savedScope && <p className="break-words text-xs text-ink-2">{savedScope}</p>}
      {timed && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-ink-3">
          <label className="flex items-center gap-2">
            Duration
            <input aria-label="Approval duration" type="number" min="1" step="1" max={cap / multiplier}
              className="w-20 rounded border border-line-strong bg-surface px-2 py-1 text-ink"
              disabled={busy} value={duration} onChange={(e) => setDuration(e.target.value)} />
          </label>
          <select aria-label="Duration unit" className="rounded border border-line-strong bg-surface px-2 py-1 text-ink"
            disabled={busy} value={unit} onChange={(e) => setUnit(e.target.value)}>
            <option value="minutes">minutes</option><option value="hours">hours</option><option value="days">days</option>
          </select>
          <span>Up to {cap} minutes{p.max_uses != null ? ` or ${p.max_uses} uses` : ""}.</span>
          <span>{expires && Number.isFinite(expires.getTime()) ? `Expires ${expires.toLocaleString()}.` : "Choose a duration within the limit."}</span>
          {!adapter && timedScope && <span className="break-words">Timed approval scope: {timedScope}</span>}
        </div>
      )}
      <span className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Permission choices">
        {choices.map((c) => (
          <button key={c.choice} type="button" className={`${ACT} ${c.choice === "deny" ? GHOST : c.choice === "allow_once" ? PRIMARY : PLAIN}`}
            disabled={busy || (c.choice === "allow_for" && timed && !valid)}
            title={(c.choice === "allow_for" ? timedScope : savedChoices.includes(c.choice) ? savedScope : null)
              ?? c.rule ?? undefined} data-choice={c.choice}
            onClick={() => actions.run(item, "permission", { ...permissionAnswer(item, c.choice),
              ...(c.choice === "allow_for" && timed ? { minutes } : {}),
              ...(savedChoices.includes(c.choice) && selectedScope ? { scope: selectedScope } : {}) })}>
            {c.choice === "allow_for" && timed ? `Approve for ${duration || "…"} ${unit}` : c.label}
          </button>
        ))}
      </span>
      {choices.some((c) => c.choice === "allow_session") && (
        <span className="text-xs text-ink-3">Session approval ends with this supervisor session{p.max_uses != null ? ` or after ${p.max_uses} uses` : ""}; it survives an app restart.</span>
      )}
      {permanent && (
        <div className="text-xs text-ink-3">
          <p>{adapter
            ? "Always allow is saved across sessions until revoked in Permissions. Allow once covers only this request."
            : "Always allow saves a permanent rule for future matching requests across sessions until removed from its permission settings."}</p>
          {!adapter && permanent.rule && <code className="block break-words">{permanent.rule}</code>}
        </div>
      )}
    </div>
  );
}

export function InboxScreen({ client, refresh, onOpen, session = null, onShowAll }: InboxScreenProps) {
  const inbox = useAppState((s) => s.inbox);
  const workspaces = useAppState((s) => s.workspaces);
  const synced = useAppState((s) => s.synced);
  const error = useAppState((s) => s.error);
  const { find, repoOf, hidden, repos } = useSessions();
  const actions = useInboxActions(client, refresh, 0, inbox);

  useEffect(() => forgetGone(inbox), [inbox]);

  const agents = useMemo(() => workspaces.flatMap((ws) => ws.agents ?? []), [workspaces]);

  // On one session: the items of any of its agents (the whole tree), none from elsewhere.
  const inSession = (i: InboxItemDTO) => session === null || (!!i.agent_id && rootOf(agents, i.agent_id, hidden) === session);
  const here = useMemo(() => (session === null ? inbox : inbox.filter(inSession)).filter((i) => !actions.hidden.has(itemKey(i))), [inbox, session, agents, hidden, actions.hidden]); // eslint-disable-line react-hooks/exhaustive-deps
  const sessionTitle = session === null ? null : (find(session)?.goal ?? session);

  /* By session: the root each item's agent hangs under, with the session's
     goal as the title. A setup item (brindle on this computer) and an org
     notice belong to no session: a group each, after the sessions. */
  const groups = useMemo<Group[]>(() => {
    const bySession = new Map<string, Group>();
    for (const item of here) {
      const root = item.agent_id ? rootOf(agents, item.agent_id, hidden) : null;
      const key = root ?? (item.agent_id ? `ws:${item.workspace_id ?? item.agent_id}` : item.kind === "setup" ? "setup" : "org");
      let group = bySession.get(key);
      if (!group) {
        if (item.kind === "setup" && !root) {
          group = { key, title: "Setup", subtitle: "brindle on this computer", items: [] };
        } else if (root) {
          const s = find(root);
          const repo = repoOf(root);
          group = { key, title: s?.goal || root, subtitle: `${root}${repo ? ` · ${repos?.find((r) => r.repo_root === repo)?.name ?? repo}` : ""}`, root, repo, items: [] };
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
    const root = item.agent_id ? rootOf(agents, item.agent_id, hidden) : null;
    const repo = root ? repoOf(root) : null;
    return root && repo ? { root, repo } : null;
  };
  /* Go to session: the item's agent in its session, its provider screen and nothing over it. */
  const goTo = (item: InboxItemDTO) => {
    const where = sessionOf(item);
    if (where && onOpen) onOpen(where.root, where.repo, item.agent_id ?? undefined);
  };

  const withSession = groups.filter((g) => g.root).length;
  return (
    <section className="relative flex min-h-0 flex-1 flex-col gap-s2" aria-label="Inbox">
      <PageHeader
        title="Inbox"
        lede={
          synced && here.length > 0
            ? `${here.length} waiting across ${withSession} session${withSession === 1 ? "" : "s"}${sessionTitle ? ` (only ${sessionTitle})` : ""}. Every card goes to its session; permissions brindle holds are answered here, the rest in the agent's terminal.`
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
      {synced && here.length === 0 && <AllClear />}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-[18px] overflow-y-auto pb-s3" aria-label="Queue">
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
            {group.items.map((item) => (
              <Banner key={itemKey(item)} item={item} actions={actions} who={item.agent_id ? agentName(workspaces, item.agent_id) : null} branch={workspaces.find((ws) => ws.id === item.workspace_id)?.branch ?? item.workspace_id ?? null} sessionTitle={group.root ? group.title : null} repo={group.repo ?? repos?.[0]?.repo_root ?? null} onDone={() => void refresh()} onGo={sessionOf(item) && onOpen ? () => goTo(item) : undefined} />
            ))}
          </section>
        ))}
      </div>
    </section>
  );
}

/* One banner card. */
function Banner({ item, actions, who, branch, sessionTitle, repo, onDone, onGo }: {
  item: InboxItemDTO;
  actions: ReturnType<typeof useInboxActions>;
  who: string | null;
  branch: string | null;
  sessionTitle: string | null;
  repo: string | null;
  onDone(): void;
  /* The agent in its session, nothing over it. */
  onGo?(): void;
}) {
  const key = itemKey(item);
  const busy = actions.busy.has(key);
  const note = actions.notes[key];
  const tone = TONE[item.kind] ?? SAP;
  const age = formatAge((Date.now() - seenAt(item)) / 1000);
  const held = item.kind === "permission" && item.permission?.via === "hook";
  const [expanded, setExpanded] = useState(false);
  const line = item.kind === "permission" ? permissionLine(item) : null;

  let body: ReactNode = null;
  if (line) {
    body = (
      <button type="button" className={`text-left ${expanded ? "whitespace-pre-wrap break-all" : "truncate"}`} title={expanded ? "Show as one line" : `${line.whole}\n\nClick to show it whole`} aria-expanded={expanded} data-testid="permission-line" onClick={() => setExpanded((e) => !e)}>
        {expanded ? (
          <code className="font-mono text-xs">{line.whole}</code>
        ) : (
          <>
            {line.lead && <span>{line.lead}: </span>}
            {line.code ? <code className="font-mono text-xs">{line.code}</code> : <span>{line.whole}</span>}
          </>
        )}
      </button>
    );
    // The grant its choices offer (Allow for …, or a built-in deny lifted once): exactly what it covers.
    const grant = item.permission?.grant;
    if (grant && (!item.permission?.approval_scope || expanded)) {
      body = (
        <span className="flex min-w-0 flex-col gap-0.5">
          {body}
          <span className="text-[11px] text-ink-3" data-testid="permission-grant">
            {grant}
          </span>
        </span>
      );
    }
  } else if (item.kind === "proposed_checks" || item.kind === "plan") {
    // Approved or accepted from this card: shown whole, never cut short.
    body = <pre className="whitespace-pre-wrap font-mono text-xs" data-testid="card-detail">{item.detail}</pre>;
  } else if (item.kind === "org_notice") {
    body = item.detail ? <span>from your org's {item.detail}</span> : null;
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
  const act = (label: string, action: InboxAction, body: Record<string, unknown> = {}, look = PRIMARY, title?: string) => (
    <button key={label} type="button" className={`${ACT} ${look}`} disabled={busy} title={title} onClick={() => actions.run(item, action, body)}>
      {label}
    </button>
  );
  let acts: ReactNode;
  if (item.kind === "permission" && answerable(item)) {
    acts = <PermissionActions key={`${item.permission?.request_id}:${item.permission?.digest}`} item={item} actions={actions} busy={busy} />;
  } else if (item.kind === "plan") {
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
  } else if (item.kind === "org_notice") {
    acts = act("Mark read", "read", {}, PLAIN);
  } else if (fixable(item)) {
    acts = <SetupFix item={item} actions={actions} onDone={onDone} />;
  } else if (item.kind === "setup") {
    acts = <span className="text-[11px] text-ink-3">fixed from a terminal: brindle doctor says how</span>;
  } else if (item.kind === "credentials" || item.kind === "login") {
    acts = <ItemActions item={item} actions={actions} repo={repo} onDone={onDone} compact />;
  } else if (item.kind === "permission") {
    acts = (
      <span className="text-[11px] text-ink-3" data-testid="permission-terminal" title={item.permission?.terminal ?? undefined}>
        answered at its own prompt, in its session
      </span>
    );
  } else {
    acts = <span className="text-[11px] text-ink-3">{item.kind === "review" || item.kind === "merge_failed" ? "handled in its session" : "answered in its session"}</span>;
  }

  return (
    <article className={`${card} relative grid max-w-[960px] grid-cols-[minmax(0,1fr)_auto] items-start gap-x-s2 gap-y-1 py-[11px] pl-[18px] pr-[14px] before:absolute before:bottom-[10px] before:left-0 before:top-[10px] before:w-[3px] before:rounded-sm ${tone.bar}`} aria-label={kindLabel(item)} data-testid="inbox-card" data-kind={item.kind}>
      <div className={`col-start-1 font-mono text-[10.5px] uppercase tracking-[0.06em] ${tone.ink}`}>
        {KIND_SHORT[item.kind] ?? item.kind}
        {held && " · held by brindle"}
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
        {onGo && (
          <button type="button" className={`${LINK} whitespace-nowrap`} onClick={onGo}>
            Go to session ›
          </button>
        )}
      </div>
    </article>
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
    <button type="button" className={`${ACT} ${PRIMARY}`} disabled={job.busy} onClick={start} data-testid="setup-fix">
      {fix.label}
    </button>
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
