/* The app: the merged sidebar (components/Sidebar.tsx): the Inbox at its
   head, the selected session's context (its goal and milestones, what it
   asks of the person, its PR status: components/SessionContext.tsx) above
   a draggable split, and under it the CLI-style tree of every open session
   and its agents; beside it the provider session fills the main area: the
   app lands straight on the last-used session's provider session (the one
   kept from the previous launch, else the first live one), or on the calm
   empty state with the New session composer when there is none
   (screens/New). No home board and no right-hand panel: the sidebar is the
   overview and brindle's layer, and the Inbox page (screens/Inbox, from
   the sidebar's head) lists every decision across sessions, each row
   jumping to its session with the item's review sheet over it, permission
   and sign-in items completable in the row. The pages behind the sidebar's foot: Knowledge (a repo's
   decisions and document sources), Agents (which model or custom agent runs
   each kind of work, what takes over at a limit, and making a custom
   agent) and Settings, one place in tabs (the plan, sign-in and spend,
   usage, keys and providers, permission rules, storage, the repo's
   settings, doctor and the engine's update). A provider's sign-in runs on the engine with a card that follows
   it (components/ProviderSignIn.tsx), and the engine's upgrade as a
   progress card (components/EngineUpgrade.tsx); the one terminal that is the
   person's own is the Terminal docked under the main column, a plain shell
   in the open repo (components/TerminalPanel.tsx, shown from the sidebar's
   status line or ⌃`). The app's own update, when one is offered, is a strip
   at the top of the main column (components/AppUpdateBanner.tsx), set aside
   into a pill in the sidebar's foot. The engine banner sits above
   the screen when the engine isn't one the app can simply use, or its
   event stream is down (components/EngineBanner.tsx). Before all of it, on
   a first launch, the full-window flow that connects the first provider
   (screens/Onboarding: no sidebar), until one is connected and signed in.
   And before that, on every launch, the sign-in gate (SignInGate below):
   nothing but brindle's sign-in until the account reads signed in. */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { DaemonError, type DaemonClient, type ProviderInstalled } from "./api/client";
import { plain } from "./api/wording";
import { DaemonContext } from "./api/context";
import { checkEngine, type EngineStatus } from "./api/engine";
import { openExternal, pickFolder as nativePicker, type OpenUrl, type PickFolder } from "./api/open";
import type { SessionDTO, WorkspaceDTO } from "./api/schema";
import type { About, AppUpdater, AppUpdates, CliLink, ReleaseBuildUpdater } from "./api/tauri";
import { AppUpdateBanner, AppUpdateProvider } from "./components/AppUpdateBanner";
import { UpgradeCard, useEngineUpgrade } from "./components/EngineUpgrade";
import { EngineBanner } from "./components/EngineBanner";
import { Sidebar } from "./components/Sidebar";
import { Walkthrough, type WalkthroughStep } from "./components/Walkthrough";
import { TerminalPanel, TerminalProvider, shellPlace } from "./components/TerminalPanel";
import { alert, button } from "./components/ui";
import { HOME, readLastSession, settingsTab, writeLastSession, type Route, type SessionRef, type SheetRef } from "./routes";
import { InboxScreen } from "./screens/Inbox";
import { KnowledgeScreen } from "./screens/Knowledge";
import { NewSession } from "./screens/New";
import { BrindleSignIn, Onboarding, Permissions, Starting, markOnboarded, onboarded, readOnboarded } from "./screens/Onboarding";
import { AgentsScreen } from "./screens/Agents";
import { SessionScreen } from "./screens/Session";
import { SettingsScreen } from "./screens/Settings";
import { isPaused } from "./store/overview";
import { SessionsProvider, useCloseSession, useSessions } from "./store/sessions";
import { ProvidersProvider } from "./store/providers";
import { UsageProvider } from "./store/usage";
import type { AppState, AppStore } from "./store/store";
import { StoreContext, useAppState, useStore } from "./store/useStore";

export interface AppProps {
  store: AppStore;
  /* The engine; null when the app has none to talk to (a plain browser tab
     without VITE_BRINDLE_DAEMON_URL). */
  client: DaemonClient | null;
  /* Fetch the snapshot and inbox again (Sync.refresh). */
  refresh?: () => Promise<void>;
  /* Stop background sync when this process cannot use the daemon. */
  onAgentRefused?: () => void;
  /* Where to open; by default the last-used session, else the first live one, else the empty state. */
  initialRoute?: Route;
  /* Opens a page in the system browser; the shell's by default. */
  openUrl?: OpenUrl;
  /* The native folder picker (Knowledge's Add folder); the shell's in the app, none in a browser tab. */
  pickFolder?: PickFolder;
  /* Puts the `brindle` command on the PATH (Settings' "Install command-line
     tool"): the shell's install_cli in the app, none in a browser tab, where
     the button stays off. */
  installCli?: () => Promise<CliLink | void>;
  /* Settings' staff-only app Update: the shell's in the app, none in a browser tab. */
  appUpdater?: AppUpdater;
  /* The app's own updates (the Inbox's Restart to update, Settings' About): the shell's in the app, none in a browser tab. */
  appUpdates?: AppUpdates;
  /* Settings' staff-only release-branch build install: the shell's in the app, none in a browser tab. */
  releaseBuildUpdater?: ReleaseBuildUpdater;
  /* Settings' About license files: the shell's in the app, none in a browser tab. */
  readAbout?: () => Promise<About>;
}

const NO_CLIENT: EngineStatus = {
  state: "unreachable",
  message: "this page has no engine. Open the app, or set VITE_BRINDLE_DAEMON_URL and _TOKEN for npm run dev.",
};

/* What the banner shows: the engine check's verdict, or, when the engine
   answers but its event stream is down, that (the store is then stale). */
export function bannerStatus(engine: EngineStatus, connection: AppState["connection"]): EngineStatus {
  if (engine.state !== "ok") return engine;
  if (connection === "unauthorized") {
    return { state: "unreachable", message: "the engine refused the app's token for its event stream. Restart the app." };
  }
  if (connection === "closed") return { state: "unreachable", message: "the event stream dropped; reconnecting…" };
  return engine;
}

/* Where the app opens once it knows its sessions: the session kept from
   last time if it is still listed, else the first live one, else the empty
   state. `last` is what the previous launch kept. */
export function landing(repos: { repo_root: string }[], sessions: Record<string, SessionDTO[]>, last: SessionRef | null): Route {
  const all = repos.flatMap((r) => (sessions[r.repo_root] ?? []).map((s) => ({ session: s, repo: r.repo_root })));
  const kept = last && all.find((x) => x.session.id === last.root && x.repo === last.repo);
  const pick = kept ?? all.find((x) => !isPaused(x.session));
  return pick ? { screen: "session", root: pick.session.id, repo: pick.repo, agent: pick.session.id } : HOME;
}

/* Agents' "Connect …" links and the Inbox's rule links: each press asks Settings to scroll (and open the rule) again. */
let providerAsks = 0;

function Screen({ route, visit, navigate, client, refresh, store, openUrl, pickFolder, installCli, appUpdater, appUpdates, releaseBuildUpdater, readAbout }: {
  route: Route;
  /* Counts the navigations (Shell's), one to the open route included. */
  visit: number;
  navigate(route: Route): void;
  client: DaemonClient;
  refresh: () => Promise<void>;
  store: AppStore;
  openUrl: OpenUrl;
  pickFolder?: PickFolder;
  installCli?: AppProps["installCli"];
  appUpdater?: AppProps["appUpdater"];
  appUpdates?: AppProps["appUpdates"];
  releaseBuildUpdater?: AppProps["releaseBuildUpdater"];
  readAbout?: AppProps["readAbout"];
}) {
  const openSession = (root: string, repo: string, agent?: string, sheet?: SheetRef) => navigate({ screen: "session", root, repo, agent, sheet });
  const closeSession = useCloseSession(navigate);
  const docsFrom = route.screen === "knowledge" ? route.from : undefined;
  switch (route.screen) {
    case "knowledge":
      return (
        <KnowledgeScreen
          key={`${route.view ?? ""}|${route.doc ?? ""}|${route.session ?? ""}`}
          client={client}
          pickFolder={pickFolder}
          view={route.view}
          doc={route.doc}
          session={route.session}
          fromRepo={docsFrom?.repo}
          onBackToSession={docsFrom && (() => navigate({ screen: "session", root: docsFrom.root, repo: docsFrom.repo, agent: docsFrom.root }))}
        />
      );
    case "agents":
      return <AgentsScreen client={client} onNavigate={(to) => navigate(to === "providers" ? { screen: "settings", section: "providers", ask: ++providerAsks } : { screen: "knowledge" })} />;
    /* One Settings, in tabs: the older Account and Project settings routes open it on their tab. */
    case "settings":
    case "project-settings":
    case "account":
      return (
        <SettingsScreen
          key={route.screen === "project-settings" ? `repo:${route.repo}` : route.screen}
          client={client}
          installCli={installCli}
          appUpdater={appUpdater}
          appUpdates={appUpdates}
          releaseBuildUpdater={releaseBuildUpdater}
          readAbout={readAbout}
          openUrl={openUrl}
          onNavigate={(screen) => navigate({ screen })}
          tab={settingsTab(route) ?? undefined}
          visit={visit}
          section={route.screen === "settings" ? route.section : undefined}
          ask={route.screen === "project-settings" ? undefined : route.ask}
          rule={route.screen === "settings" ? route.rule : undefined}
          repo={route.screen === "project-settings" ? route.repo : undefined}
          current={readLastSession()?.repo}
        />
      );
    case "new":
      return <NewSession key={route.repo} client={client} onOpen={openSession} initialRepo={route.repo} pickFolder={pickFolder} refresh={refresh} onSignIn={() => navigate({ screen: "settings", section: "providers", ask: ++providerAsks })} />;
    case "inbox":
      return <InboxScreen client={client} refresh={refresh} onOpen={openSession} onNavigate={navigate} session={route.session ?? null} onShowAll={() => navigate({ screen: "inbox" })} onOpenRule={(rule) => navigate({ screen: "settings", section: "permissions", rule, ask: ++providerAsks })} appUpdates={appUpdates} openUrl={openUrl} />;
    case "session":
      return (
        <SessionScreen
          key={route.root}
          client={client}
          root={route.root}
          repo={route.repo}
          refresh={refresh}
          store={store}
          agent={route.agent ?? route.root}
          sheet={route.sheet ?? null}
          onSelect={(agent, sheet) => navigate({ ...route, agent, sheet: sheet ?? undefined })}
          openUrl={openUrl}
          onOpen={openSession}
          onCloseSession={closeSession}
        />
      );
  }
}

/* The banner's "Upgrade the engine" runs the upgrade on the engine in the
   background, as a progress card under the banner; done, the engine is
   checked again (the new daemon answers). */
function Banner({ status, connection, onRetry, client, store }: { status: EngineStatus; connection: AppState["connection"]; onRetry(): void; client: DaemonClient; store: AppStore }) {
  const upgrade = useEngineUpgrade(client, store);
  const done = upgrade.state.kind === "done";
  useEffect(() => {
    if (done) onRetry();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [done]);
  const kind = upgrade.state.kind;
  return (
    <>
      <EngineBanner
        status={status}
        connection={connection}
        onRetry={onRetry}
        onUpgrade={() => void upgrade.start()}
        upgrading={kind === "starting" || kind === "running" ? "running" : kind === "failed" ? "failed" : "idle"}
      />
      {upgrade.state.kind !== "idle" && (
        <div className="border-b border-line px-s3 py-s1">
          <UpgradeCard state={upgrade.state} onDismiss={upgrade.dismiss} />
        </div>
      )}
    </>
  );
}

/* `firstRun`: the app opens right after the first launch's setup, so the walkthrough offers itself. */
function Shell({ store, client, refresh, initialRoute, openUrl = openExternal, pickFolder = nativePicker, installCli, appUpdater, appUpdates, releaseBuildUpdater, readAbout, firstRun = false }: AppProps & { firstRun?: boolean }) {
  // Until the sessions are known the app shows the empty state, quietly; then it lands.
  const [route, setRoute] = useState<Route | null>(initialRoute ?? null);
  /* Counts the navigations: one to the route already open still counts (Settings opens its tab again). */
  const [visit, setVisit] = useState(0);
  const navigate = useCallback((to: Route) => {
    setVisit((n) => n + 1);
    setRoute(to);
  }, []);
  const { repos, sessions, error: feedError, reload } = useSessions();
  const showWalkthroughStep = useCallback((step: WalkthroughStep) => {
    switch (step.target) {
      case "new-session":
      case "project-picker":
        // Keep the selected project and draft when both steps describe the composer.
        setRoute((current) => current?.screen === "new" ? current : HOME);
        break;
      case "inbox":
        navigate({ screen: "inbox" });
        break;
      case "profile":
        navigate({ screen: "account" });
        break;
      case "session-view":
        // With no session yet, show the empty session area and its composer.
        navigate(landing(repos ?? [], sessions, readLastSession()));
        break;
    }
  }, [navigate, repos, sessions]);
  useEffect(() => {
    // A place the person already picked (the sidebar answers before the sessions do) stays.
    if (route === null && repos !== null) setRoute((picked) => picked ?? landing(repos, sessions, readLastSession()));
  }, [route, repos, sessions]);
  // The session on screen is the one the next launch opens on.
  useEffect(() => {
    if (route?.screen === "session") writeLastSession({ root: route.root, repo: route.repo });
  }, [route]);

  const error = useAppState((s) => s.error);
  const connection = useAppState((s) => s.connection);

  const [engine, setEngine] = useState<EngineStatus>(client ? { state: "connecting" } : NO_CLIENT);
  const check = useCallback(() => {
    if (client) void checkEngine(client).then(setEngine);
  }, [client]);
  // On start (whatever the stream is doing: an unreachable engine never opens
  // it), and again whenever the stream (re)opens: an upgraded engine comes
  // back as a new daemon.
  useEffect(check, [check]);
  useEffect(() => {
    if (connection === "open") check();
  }, [check, connection]);

  const noop = useCallback(() => Promise.resolve(), []);

  // A session's view and the empty state keep their own height (the surface
  // fills it, a sheet lies over it), the Inbox too (its queue scrolls, a
  // sheet lies over it); the pages scroll.
  const shown = route ?? HOME;
  const fills = shown.screen === "session" || shown.screen === "new" || shown.screen === "inbox";
  // A visual open in the session takes the whole window (design/visualizer-build-plan-amendments.md
  // item 1): the sidebar hides, still mounted, so Back finds it as it was (folds, scroll, split).
  const reading = shown.screen === "session" && shown.sheet?.kind === "visual";
  // Where a shell of the person's own starts: the open session's worktree, else the repo's root.
  const workspaces = useAppState((s) => s.workspaces);
  const here = useMemo(() => shellPlace(shown, repos ?? [], workspaces, shown.screen === "session" ? sessions[shown.repo]?.find((s) => s.id === shown.root) : null), [shown, repos, workspaces, sessions]);
  return (
    <TerminalProvider client={client} here={here}>
      <AppUpdateProvider updates={appUpdates}>
      <div className="flex h-full bg-canvas text-ink">
        <div className="contents" hidden={reading}>
          <Sidebar route={shown} navigate={navigate} refresh={refresh ?? noop} openUrl={openUrl} />
        </div>
        <div className="relative flex min-w-0 flex-1 flex-col">
          <AppUpdateBanner openUrl={openUrl} />
          {/* The banner's upgrade follows the engine's terminal stream, which a page with no engine doesn't have. */}
          {client ? <Banner status={bannerStatus(engine, connection)} connection={connection} onRetry={check} client={client} store={store} /> : <EngineBanner status={bannerStatus(engine, connection)} connection={connection} onRetry={check} />}
          <main data-walkthrough="session-view" className={`relative flex min-h-0 flex-1 flex-col ${fills ? "overflow-hidden" : "gap-s2 overflow-auto p-s3"}`}>
            {error && <p className={`${alert} ${fills ? "m-s2 mb-0" : ""}`}>{error}</p>}
            {client ? (
              route ? (
                <div className={`relative flex min-h-0 flex-1 flex-col ${shown.screen === "inbox" ? "gap-s2 p-s3" : ""}`}>
                  <Screen route={route} visit={visit} navigate={navigate} client={client} refresh={refresh ?? noop} store={store} openUrl={openUrl} pickFolder={pickFolder} installCli={installCli} appUpdater={appUpdater} appUpdates={appUpdates} releaseBuildUpdater={releaseBuildUpdater} readAbout={readAbout} />
</div>
              ) : feedError ? (
                // Nothing to land on: the sessions couldn't be read (the engine itself may be fine). Ask again.
                <div role="alert" className={`${alert} m-s3 flex items-center gap-s2`} data-testid="sessions-failed">
                  <span>Couldn't load the sessions: {feedError}</span>
                  <button type="button" className={button} onClick={() => void reload()}>
                    Retry
                  </button>
                </div>
              ) : (
                <p className="p-s3 text-ink-3">Loading…</p>
              )
            ) : (
              <p className="p-s3 text-ink-3">Nothing to show without an engine.</p>
            )}
          </main>
          {/* Docked under the session (or whatever page is shown): the screen above gets shorter, nothing lies over it. */}
          <TerminalPanel />
        </div>
        {client && <Walkthrough autoStart={firstRun} onStep={showWalkthroughStep} />}
      </div>
      </AppUpdateProvider>
    </TerminalProvider>
  );
}

/* The first launch's gate: the app asks the engine which providers it has
   (GET /providers/installed) behind "Starting brindle…" (the same screen
   Launch shows while the engine starts), and until one is connected and
   signed in the first launch's full-window flow shows instead of the app
   (screens/Onboarding). Later launches go straight in; so does an engine
   without the route (an older one: 404), whose banner speaks for it. Any
   other failure (the network, a 500) says so in Starting's place, with
   Retry: a first launch never skips its setup on a hiccup. */
/* The flow done, the app opens on New session (no demo offer on the first launch),
   with the walkthrough (components/Walkthrough) over it once. */
type Gate =
  | { kind: "checking"; error?: string }
  | { kind: "onboarding"; providers: ProviderInstalled[] }
  | { kind: "permissions" }
  /* `fresh`: reached through the first launch's setup just now (the walkthrough offers itself). */
  | { kind: "app"; route?: Route; fresh?: boolean };

function FirstLaunch(props: AppProps) {
  const { client, openUrl = openExternal, pickFolder = nativePicker } = props;
  // A first launch done on this Mac before (api/providers.ts): straight in, nothing to ask.
  const [gate, setGate] = useState<Gate>(client && !readOnboarded() ? { kind: "checking" } : { kind: "app" });
  /* Asks done so far; Retry asks again. */
  const [asked, setAsked] = useState(0);
  const asking = gate.kind === "checking";
  useEffect(() => {
    if (!client || !asking) return;
    let live = true;
    client.providersInstalled().then(
      (rows) => {
        if (!live) return;
        if (!onboarded(rows)) return setGate({ kind: "onboarding", providers: rows });
        // The CLI is ready, but this Mac's first app launch still offers permissions.
        setGate({ kind: "permissions" });
      },
      (e: unknown) => {
        if (!live) return;
        if (e instanceof DaemonError && e.status === 404) setGate({ kind: "app" });
        else setGate({ kind: "checking", error: plain(e instanceof Error ? e.message : String(e)) });
      },
    );
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, asked]);
  const done = useCallback((route?: Route) => setGate({ kind: "app", route, fresh: true }), []);
  const permissionsDone = useCallback(() => {
    markOnboarded();
    done();
  }, [done]);
  if (gate.kind === "checking") {
    const retry = () => {
      setGate({ kind: "checking" });
      setAsked((n) => n + 1);
    };
    return <Starting error={gate.error} onRetry={retry} />;
  }
  if (gate.kind === "onboarding" && client) {
    return <Onboarding client={client} providers={gate.providers} openUrl={openUrl} pickFolder={pickFolder} onDone={done} />;
  }
  if (gate.kind === "permissions" && client) {
    return <Permissions client={client} onNext={permissionsDone} />;
  }
  // The providers feed starts with the app itself: the first launch reads its own rows.
  return (
    <ProvidersProvider client={client}>
      <Startup client={client} refresh={props.refresh}>
        <Shell {...props} initialRoute={(gate.kind === "app" && gate.route) || props.initialRoute} firstRun={gate.kind === "app" && gate.fresh} />
      </Startup>
    </ProvidersProvider>
  );
}

const STARTUP_WAIT = 30000;
const STARTUP_CHECK = 500;

function waitingForPanes(workspaces: WorkspaceDTO[]): boolean {
  return workspaces.flatMap((ws) => ws.agents ?? []).some((a) =>
    a.provider !== "subagent" && !a.headless && !a.locked
    && !["paused", "done", "failed", "closed", "rewound", "exited"].includes(a.status) && !a.window);
}

/* Mount the shell only once recovery has settled. Reconnects belong to its
   banner, never to this launch gate. The deadline also bounds a hung read. */
function Startup({ client, refresh, children }: { client: DaemonClient | null; refresh?: () => Promise<void>; children: ReactNode }) {
  const store = useStore();
  const [done, setDone] = useState(!client);
  const [line, setLine] = useState("Connecting to brindle…");
  const { reload } = useSessions();
  useEffect(() => {
    if (!client || done) return;
    let current = true;
    let next: ReturnType<typeof setTimeout>;
    const deadline = setTimeout(() => {
      current = false;
      clearTimeout(next);
      setDone(true);
    }, STARTUP_WAIT);
    const check = async () => {
      const { lastEventId, connectionGeneration } = store.getState();
      const unchanged = () => current && store.getState().lastEventId === lastEventId && store.getState().connectionGeneration === connectionGeneration;
      try {
        const meta = await client.meta();
        if (!unchanged()) return;
        setLine(meta.resuming ? "Bringing your sessions back…" : "Checking your sessions…");
        if (!meta.resuming) {
          const workspaces = await client.snapshot();
          if (!unchanged()) return;
          if (!waitingForPanes(workspaces)) {
            // A snapshot read before recovery may still be in flight in sync.
            // Queue a fresh one before mounting terminals on the store's rows.
            const version = store.getState().snapshotVersion;
            if (refresh) await Promise.all([reload(), refresh()]);
            else {
              const inbox = await client.inbox();
              if (!unchanged()) return;
              store.dispatch({ type: "snapshot", workspaces, inbox });
              await reload();
            }
            if (!unchanged()) return;
            // Recovery can start in another daemon while the reads finish.
            const latest = await client.meta();
            if (!unchanged() || latest.resuming || latest.pid !== meta.pid) return;
            const applied = store.getState();
            // Sync reports failures in the store and still resolves its promise.
            // Only newly applied, ready rows may be used to mount terminals.
            if (applied.snapshotVersion === version || applied.error || applied.stale || waitingForPanes(applied.workspaces)) return;
            current = false;
            clearTimeout(deadline);
            setDone(true);
            return;
          }
          setLine("Waiting for your sessions to open…");
        }
      } catch {
        if (!unchanged()) return;
        setLine("Waiting for brindle to answer… You’ll be let in shortly.");
      } finally {
        if (current) next = setTimeout(() => void check(), STARTUP_CHECK);
      }
    };
    void check();
    return () => {
      current = false;
      clearTimeout(next);
      clearTimeout(deadline);
    };
  }, [client, done, reload, refresh, store]);
  return done ? <>{children}</> : <Starting line={line} />;
}

/* The sign-in gate: the app needs a brindle account. Once the engine
   answers (Launch is past "ready" when App mounts), the app asks it for the
   account (GET /account) and, until that reads signed in, shows brindle's
   sign-in (Onboarding's step 1, BrindleSignIn) and nothing else: no
   onboarding, no sidebar, whatever `brindle.onboarded` or the providers
   say. A read that fails (an engine without the route, the network) says
   so on the same screen with Retry; it never falls through to the app.
   Signed in, the first launch's own gate (FirstLaunch) takes over. A
   sign-out later (the Account page's, which puts the account it reads
   into the store) brings the gate back. A page with no engine has no
   account to ask about: it goes on to say so. */
type Signed = { kind: "checking" } | { kind: "error"; error: string } | { kind: "refused" } | { kind: "out" } | { kind: "in" };

function SignInGate({ client, openUrl = openExternal, onAgentRefused, children }: {
  client: DaemonClient | null;
  openUrl?: OpenUrl;
  onAgentRefused?: () => void;
  children: ReactNode;
}) {
  const store = useStore();
  const [signed, setSigned] = useState<Signed>(client ? { kind: "checking" } : { kind: "in" });
  /* Reads asked so far: Retry and a finished sign-in ask again. */
  const [asked, setAsked] = useState(0);
  const checking = signed.kind === "checking";
  useEffect(() => {
    if (!client || !checking) return;
    let live = true;
    client.account().then(
      (a) => {
        if (!live) return;
        // The store's copy first: the sidebar and the sign-out watch below read it.
        store.dispatch({ type: "account", account: a });
        setSigned(a.signed_in ? { kind: "in" } : { kind: "out" });
      },
      (e: unknown) => {
        if (!live) return;
        // Identity is checked against process ancestry. This launch cannot
        // recover through sign-in or by clearing its inherited environment.
        if (e instanceof DaemonError && e.code === "agent_caller") {
          onAgentRefused?.();
          setSigned({ kind: "refused" });
        } else {
          setSigned({ kind: "error", error: plain(e instanceof Error ? e.message : String(e)) });
        }
      },
    );
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, asked]);
  const ask = useCallback(() => {
    setSigned({ kind: "checking" });
    setAsked((n) => n + 1);
  }, []);
  // Signed out from inside the app: back to the gate.
  const out = useAppState((s) => s.account?.signed_in === false);
  useEffect(() => {
    if (out && signed.kind === "in") setSigned({ kind: "out" });
  }, [out, signed.kind]);

  if (signed.kind === "in" || !client) return <>{children}</>;
  if (signed.kind === "refused") {
    return (
      <div className="flex h-full items-center justify-center p-s4">
        <p className="max-w-prose text-ink-2" role="alert">
          brindle was opened from an agent's terminal. Open it from the Dock
        </p>
      </div>
    );
  }
  if (checking) return <Starting />;
  const problem = signed.kind === "error" && (
    <div className={`${alert} flex flex-wrap items-center gap-s2`} role="alert" data-testid="signin-gate-failed">
      <span className="min-w-0 flex-1">Couldn't check your brindle account: {signed.error}</span>
      <button type="button" className={button} onClick={ask}>
        Retry
      </button>
    </div>
  );
  return (
    <div className="h-full" data-testid="signin-gate">
      {/* A first launch's step 1 of the flow that follows; on later launches, just the account. */}
      <BrindleSignIn client={client} openUrl={openUrl} onSignedIn={ask} kicker={readOnboarded() ? "brindle account" : "Step 1 of 3"} problem={problem} />
    </div>
  );
}

export default function App(props: AppProps) {
  return (
    <StoreContext.Provider value={props.store}>
      <DaemonContext.Provider value={props.client}>
        <SignInGate client={props.client} openUrl={props.openUrl} onAgentRefused={props.onAgentRefused}>
          <SessionsProvider client={props.client}>
            <UsageProvider client={props.client}>
              <FirstLaunch {...props} />
            </UsageProvider>
          </SessionsProvider>
        </SignInGate>
      </DaemonContext.Provider>
    </StoreContext.Provider>
  );
}
