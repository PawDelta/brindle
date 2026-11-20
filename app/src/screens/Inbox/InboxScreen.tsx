/* The Inbox page: what the team needs from you, in one place, as a queue you
   clear (InboxQueue.tsx, design inbox-redesign.html): Waiting on you first
   (an agent or a person is stopped until you act), its first item open with
   its answer box; then From the team (nothing is stopped); then the day's
   Done, folded away. Every row acts where it is read, one click sent at once
   (POST /inbox/{id}/{action}); the engine's refusal, if any, comes back as
   one line on the row.

   The queue is behind the inbox_queue_v2 switch (store/switches.tsx), on by
   default; off, the earlier cards by session (InboxCards.tsx).

   Permissions are one card (PermissionAsks.tsx), whoever holds them, first
   under Waiting on you: Deny, Allow once, Allow ▾ for a request brindle's
   hook holds, a note and a way to the agent's window for a prompt only that
   window can take. Requests brindle's hook holds come grouped: one card per
   kind and general form, decided at once for every request in it, under the
   stall banner.

   Above the queue, the app's own item (AppUpdateItem.tsx): an update the
   app installed by itself, with Restart to update, then "Updated to
   brindle X" once. On the whole Inbox, a quiet line (StorageNudge.tsx)
   when brindle could give back over 5 GB of disk, to Settings › Storage. */

import { useEffect, useMemo, useRef } from "react";
import type { DaemonClient } from "../../api/client";
import { openExternal, type OpenUrl } from "../../api/open";
import type { AppUpdates } from "../../api/tauri";
import type { InboxItemDTO } from "../../api/schema";
import { PageHeader } from "../../components/PageHeader";
import type { SheetRef } from "../../routes";
import { alert, button, quiet } from "../../components/ui";
import { agentName, rootOf } from "../../store/overview";
import { useSessions } from "../../store/sessions";
import { useSwitch } from "../../store/switches";
import { useAppState } from "../../store/useStore";
import type { Route } from "../../routes";
import { appUpdateItem } from "../../store/appUpdates";
import { AppUpdateCard } from "./AppUpdateItem";
import { InboxCards } from "./InboxCards";
import { InboxQueue, type QueueContext } from "./InboxQueue";
import { PermissionAsks, PermissionItemCard, usePermissionAsks } from "./PermissionAsks";
import { StorageNudge } from "./StorageNudge";
import { askKey, forgetGone, itemKey, markShown, opensVisual, pruneShown, seenKey, shownKeys, unseenKeys } from "./items";
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

export function InboxScreen(props: InboxScreenProps) {
  return useSwitch("inbox_queue_v2") ? <InboxQueueScreen {...props} /> : <InboxCards {...props} />;
}

function InboxQueueScreen({ client, refresh, onOpen, onNavigate, session = null, onShowAll, onOpenRule, appUpdates, openUrl = openExternal }: InboxScreenProps) {
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
  const rootFor = (i: InboxItemDTO) => i.root_id ?? (i.agent_id ? rootOf(agents, i.agent_id, hidden) : null);

  // On one session: the items of any of its agents (the whole tree), none from elsewhere.
  // A request the permission items hold is theirs, not a row of its own.
  const inSession = (i: InboxItemDTO) => session === null || rootFor(i) === session;
  const here = useMemo(() => (session === null ? inbox : inbox.filter(inSession)).filter((i) => !actions.hidden.has(itemKey(i)) && !asks.held(i)), [inbox, session, agents, hidden, actions.hidden, asks.held]); // eslint-disable-line react-hooks/exhaustive-deps
  const askGroups = useMemo(() => asks.groups
    .map((g) => ({ ...g, items: g.items.filter((a) => !pastWorkers.includes(a.agent_id) && (session === null || a.root_id === session)) }))
    .filter((g) => g.items.length > 0), [asks.groups, session, pastWorkers]);
  const permissions = here.filter((i) => i.kind === "permission");
  const queued = here.filter((i) => i.kind !== "permission");

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

  const sessionOf = (item: InboxItemDTO) => {
    const root = rootFor(item);
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

  const repoRoot = (item: InboxItemDTO) => {
    const root = rootFor(item);
    return (root ? repoOf(root) : null) ?? repos?.[0]?.repo_root ?? null;
  };
  const working = agents.filter((a) => a.status === "processing");
  const ctx: QueueContext = {
    who: (item) => (item.agent_id ? agentName(workspaces, item.agent_id) : null),
    branch: (item) => workspaces.find((ws) => ws.id === item.workspace_id)?.branch ?? null,
    sessionTitle: (item) => {
      const root = rootFor(item);
      const s = root ? find(root) : undefined;
      return s ? s.name || s.goal || root : null;
    },
    repo: repoRoot,
    repoName: (item) => {
      const root = rootFor(item);
      const repo = root ? repoOf(root) : null;
      return repo ? repos?.find((r) => r.repo_root === repo)?.name ?? repo.split("/").pop() ?? repo : null;
    },
    goTo: (item) => (sessionOf(item) && onOpen ? () => goTo(item) : undefined),
    review: (item) => (sessionOf(item) && onOpen ? () => goTo(item, { kind: "review" }) : undefined),
    onNavigate,
    openUrl,
    working: { agents: working.length, sessions: new Set(working.map((a) => rootOf(agents, a.id, hidden) ?? a.id)).size },
  };

  const permissionCards = (askGroups.length > 0 || permissions.length > 0) && (
    <div className="flex flex-col gap-s1 pb-s1">
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
      {permissions.map((item) => (
        <PermissionItemCard key={itemKey(item)} item={item} isNew={fresh.has(seenKey(item))} actions={actions} who={item.agent_id ? agentName(workspaces, item.agent_id) : "An agent"} onGo={sessionOf(item) && onOpen ? () => goTo(item) : undefined} />
      ))}
    </div>
  );

  return (
    <section className="relative flex min-h-0 flex-1 flex-col gap-[18px]" aria-label="Inbox">
      <PageHeader title="Inbox" lede="What the team needs from you, in one place.">
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
      <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-[18px] overflow-y-auto pb-s3" aria-label="Queue">
        {update && appUpdates && (
          <section className="flex max-w-[920px] flex-col gap-s1" aria-label="This app">
            <AppUpdateCard key={`${update.kind}:${update.version}`} item={update} updates={appUpdates} openUrl={openUrl} />
          </section>
        )}
        {session === null && <StorageNudge client={client} onNavigate={onNavigate} />}
        {(synced || !error) && (
          <InboxQueue
            items={queued}
            synced={synced}
            fresh={fresh}
            actions={actions}
            client={client}
            refresh={refresh}
            ctx={ctx}
            waitingExtra={permissionCards}
            waitingExtraCount={askGroups.length + permissions.length}
          />
        )}
      </div>
    </section>
  );
}
