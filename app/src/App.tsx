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
   and sign-in items completable in the row. The Library (designs, a repo's
   decisions and document sources) opens from the sidebar foot's button or ⌘L and takes the
   sidebar for its navigation (components/LibraryRail.tsx). The pages behind the profile button's rail: Agents (which model or custom agent runs
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
   event stream is down (components/EngineBanner.tsx); when an engine that
   answered goes away (a restart), the last screen stays under a calm
   reconnect overlay instead, until it answers again
   (components/ReconnectOverlay.tsx). The shell shows as soon as the
   account is known: only agents' panes wait for the engine to bring its
   sessions back (usePanesReady below). Before all of it, on
   a first launch, the full-window flow that connects the first provider
   (screens/Onboarding: no sidebar), until one is connected and signed in.
   And before that, on every launch, the sign-in gate (SignInGate below):
   nothing but brindle's sign-in until the account reads signed in. */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { DaemonError, type DaemonClient, type Meta, type ProviderInstalled } from "./api/client";
import { plain } from "./api/wording";
import { startupStep } from "./api/startup";
import { DaemonContext } from "./api/context";
import { checkEngine, type EngineStatus } from "./api/engine";
import { openExternal, pickFolder as nativePicker, type OpenUrl, type PickFolder } from "./api/open";
import type { SessionDTO, WorkspaceDTO } from "./api/schema";
import type { About, AppUpdater, AppUpdates, CliLink, ReleaseBuildUpdater } from "./api/tauri";
import { AppUpdateBanner, AppUpdateProvider } from "./components/AppUpdateBanner";
import { UpgradeCard, useEngineUpgrade } from "./components/EngineUpgrade";
import { EngineBanner } from "./components/EngineBanner";
import Logo from "./components/Logo";
import { AppCovered, ReconnectOverlay, useReconnect } from "./components/ReconnectOverlay";
import type { LibraryCounts } from "./components/LibraryRail";
import { Sidebar } from "./components/Sidebar";
import { Walkthrough, type WalkthroughStep } from "./components/Walkthrough";
import { TerminalPanel, TerminalProvider, shellPlace } from "./components/TerminalPanel";
import { alert, button } from "./components/ui";
import { escapeOwned } from "./escape";
import { HOME, inLibrary, inProfileRail, inRail, libraryView, readLastSession, settingsTab, writeLastSession, type Route, type SessionRef, type SheetRef } from "./routes";
import { InboxScreen } from "./screens/Inbox";
import { KnowledgeScreen } from "./screens/Knowledge";
import { SharedDesignReader } from "./screens/Session/Visuals/SharedDesignReader";
import { NewSession } from "./screens/New";
import { BrindleSignIn, Onboarding, Permissions, Starting, markOnboarded, onboarded, readOnboarded } from "./screens/Onboarding";
import { AgentsScreen } from "./screens/Agents";
import { SessionScreen } from "./screens/Session";
import { SettingsScreen } from "./screens/Settings";
import { isPaused } from "./store/overview";
import { SessionsProvider, useCloseSession, useSessions } from "./store/sessions";
import { PanesReadyContext, type PanesReady } from "./store/panes";
import { agentIds, sessionTree } from "./screens/Session/tree";
import { ProvidersProvider } from "./store/providers";
import { UsageProvider } from "./store/usage";
import { snapshotTicket, type AppState, type AppStore } from "./store/store";
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
  /* The native folder picker (the Library's Add folder); the shell's in the app, none in a browser tab. */
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

function Screen({ route, visit, navigate, client, refresh, store, openUrl, pickFolder, installCli, appUpdater, appUpdates, releaseBuildUpdater, readAbout, onLibraryCounts }: {
  /* The Library's screen says what it holds, for its sidebar's counts. */
  onLibraryCounts?(counts: LibraryCounts): void;
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
  const openSession = (root: string, repo: string, agent?: string, sheet?: SheetRef) => navigate({
    screen: "session",
    root,
    repo,
    agent,
    sheet,
  });
  const closeSession = useCloseSession(navigate);
  const docsFrom = route.screen === "knowledge" ? route.from : undefined;
  switch (route.screen) {
    case "knowledge":
      return (
        <KnowledgeScreen
          key={`${route.doc ?? ""}|${route.session ?? ""}`}
          client={client}
          pickFolder={pickFolder}
          view={route.view}
          stage={route.stage}
          folder={route.folder}
          inRepo={route.repo}
          onView={(view) => navigate({ ...route, view })}
          onCounts={onLibraryCounts}
          doc={route.doc}
          session={route.session}
          fromRepo={docsFrom?.repo}
          onBackToSession={docsFrom && (() => navigate({ screen: "session", root: docsFrom.root, repo: docsFrom.repo, agent: docsFrom.root }))}
        />
      );
    case "shared-design":
      return <SharedDesignReader key={route.doc} client={client} docId={route.doc} title={route.title} repo={route.repo} onBack={() => navigate({ screen: "inbox" })} />;
    case "agents":
      return <AgentsScreen client={client} onNavigate={(to) => navigate(to === "providers" ? { screen: "settings", section: "providers", ask: ++providerAsks } : { screen: "knowledge" })} />;
    /* One Settings, in tabs: the older Account and Project settings routes open it on their tab. */
    case "settings":
    case "project-settings":
    case "account":
      return (
        <SettingsScreen
          client={client}
          installCli={installCli}
          appUpdater={appUpdater}
          appUpdates={appUpdates}
          releaseBuildUpdater={releaseBuildUpdater}
          readAbout={readAbout}
          openUrl={openUrl}
          onNavigate={(screen) => navigate({ screen })}
          onTab={(tab) => navigate({ screen: "settings", tab, repo: "repo" in route ? route.repo : undefined })}
          tab={settingsTab(route) ?? undefined}
          visit={visit}
          section={route.screen === "settings" ? route.section : undefined}
          ask={route.screen === "project-settings" ? undefined : route.ask}
          rule={route.screen === "settings" ? route.rule : undefined}
          repo={route.screen === "project-settings" || route.screen === "settings" ? route.repo : undefined}
          current={readLastSession()?.repo}
        />
      );
    case "new":
      return <NewSession key={`${route.repo ?? ""}|${route.add ?? ""}|${route.add ? visit : ""}`} client={client} onOpen={openSession} initialRepo={route.repo} add={route.add} pickFolder={pickFolder} refresh={refresh} onSignIn={() => navigate({ screen: "settings", section: "providers", ask: ++providerAsks })} />;
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
          onHome={() => navigate({ screen: "new" })}
          onStartOwn={(add) => navigate({ screen: "new", add })}
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
  useEffect(() => {
    // The second frame runs after the browser has had a paint opportunity.
    let painted = 0;
    const frame = requestAnimationFrame(() => {
      painted = requestAnimationFrame(() => startupStep("shell_painted"));
    });
    return () => { cancelAnimationFrame(frame); cancelAnimationFrame(painted); };
  }, []);
  // Until the sessions are known the app shows the empty state, quietly; then it lands.
  const [route, setRoute] = useState<Route | null>(initialRoute ?? null);
  /* Counts the navigations: one to the route already open still counts (Settings opens its tab again). */
  const [visit, setVisit] = useState(0);
  const page = useRef<HTMLElement>(null);
  const routeNow = useRef(route);
  routeNow.current = route;
  const [settingsFrom, setSettingsFrom] = useState<{ route: Route; visit: number; scroll: number } | null>(null);
  /* What the open Library holds, as its screen read it: the counts in its sidebar. */
  const [libraryCounts, setLibraryCounts] = useState<LibraryCounts>({});
  const noteLibraryCounts = useCallback((now: LibraryCounts) => setLibraryCounts((was) => (was.decisions === now.decisions && was.imported === now.imported && was.kept === now.kept ? was : now)), []);
  const visitNow = useRef(visit);
  visitNow.current = visit;
  const restoreScroll = useRef<number | null>(null);
  /* The Settings page the Library was opened from, if it was: where its button and ⌘L go back to. */
  const libraryFrom = useRef<Route | null>(null);
  const navigate = useCallback((to: Route) => {
    const from = routeNow.current;
    if (inRail(to) && (!from || !inRail(from))) {
      setSettingsFrom({ route: from ?? HOME, visit: visitNow.current, scroll: page.current?.scrollTop ?? 0 });
      restoreScroll.current = 0;
    } else if (!inRail(to)) {
      setSettingsFrom(null);
    }
    if (inLibrary(to) && (!from || !inLibrary(from))) libraryFrom.current = from && inProfileRail(from) ? from : null;
    setVisit((n) => n + 1);
    setRoute(to);
  }, []);
  const backFromSettings = useCallback(() => {
    restoreScroll.current = settingsFrom?.scroll ?? 0;
    navigate(settingsFrom?.route ?? HOME);
  }, [navigate, settingsFrom]);
  /* The sidebar foot's Library button and ⌘L: open the Library, or from it, go back to where the person was. */
  const toggleLibrary = useCallback(() => {
    const now = routeNow.current;
    if (!now || !inLibrary(now)) navigate({ screen: "knowledge" });
    else if (libraryFrom.current) navigate(libraryFrom.current);
    else backFromSettings();
  }, [navigate, backFromSettings]);
  useLayoutEffect(() => {
    if (restoreScroll.current !== null && page.current) {
      page.current.scrollTop = restoreScroll.current;
      restoreScroll.current = null;
    }
  }, [route]);
  useEffect(() => {
    if (!route || !inRail(route)) return;
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented || escapeOwned()) return;
      event.preventDefault();
      backFromSettings();
    };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [route, backFromSettings]);
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
        if (repos === null) return false;
        // With no session yet, show the empty session area and its composer.
        navigate(landing(repos, sessions, readLastSession()));
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
  // The engine answered before and now doesn't (a restart): the last screen stays, under the reconnect overlay.
  const { phase: reconnect, focusBefore } = useReconnect(connection);
  const away = reconnect !== "none";
  const held = reconnect === "shown" || reconnect === "long";

  // A session's view and the empty state keep their own height (the surface
  // fills it, a sheet lies over it), the Inbox too (its queue scrolls, a
  // sheet lies over it); the pages scroll.
  const shown = route ?? HOME;
  // The Library's designs fill it too: their table header and results line stay put while the rows scroll.
  const fills = shown.screen === "session" || shown.screen === "new" || shown.screen === "inbox" || (shown.screen === "knowledge" && libraryView(shown) === "docs");
  // A visual open in the session takes the whole window (design/visualizer-build-plan-amendments.md
  // item 1): the sidebar hides, still mounted, so Back finds it as it was (folds, scroll, split).
  const reading = shown.screen === "session" && shown.sheet?.kind === "visual";
  // Where a shell of the person's own starts: the open session's worktree, else the repo's root.
  const workspaces = useAppState((s) => s.workspaces);
  const here = useMemo(() => shellPlace(shown, repos ?? [], workspaces, shown.screen === "session" ? sessions[shown.repo]?.find((s) => s.id === shown.root) : null), [shown, repos, workspaces, sessions]);
  return (
    <TerminalProvider client={client} here={here}>
      <AppUpdateProvider updates={appUpdates}>
      {/* A visual's native page sits above the page: under the overlay it steps aside too. */}
      <AppCovered.Provider value={held}>
      <div className="relative flex h-full bg-canvas text-ink">
        {/* Under the reconnect overlay the app stays on screen but out of reach: no focus, no clicks. */}
        <div className="contents" hidden={reading} inert={held}>
          <Sidebar route={shown} navigate={navigate} onBackFromSettings={backFromSettings} onToggleLibrary={toggleLibrary} libraryCounts={libraryCounts} refresh={refresh ?? noop} openUrl={openUrl} />
        </div>
        <div className="relative flex min-w-0 flex-1 flex-col" inert={held}>
          {/* The sidebar is hidden over a full-window visual: its logo keeps a row of its own above the reader, and still goes Home. */}
          {reading && (
            <div className="flex shrink-0 items-center px-s3 pt-s2">
              <Logo className="text-[17px]" onHome={() => navigate({ screen: "new" })} />
            </div>
          )}
          <AppUpdateBanner openUrl={openUrl} />
          {/* The banner's upgrade follows the engine's terminal stream, which a page with no engine doesn't have.
              While the engine is away after having answered, the overlay speaks for it instead (no raw error strip). */}
          {away ? null : client ? <Banner status={bannerStatus(engine, connection)} connection={connection} onRetry={check} client={client} store={store} /> : <EngineBanner status={bannerStatus(engine, connection)} connection={connection} onRetry={check} />}
          <main ref={page} data-walkthrough="session-view" className={`relative flex min-h-0 flex-1 flex-col ${fills ? "overflow-hidden" : "gap-s2 overflow-auto p-s3"}`}>
            {error && !away && <p className={`${alert} ${fills ? "m-s2 mb-0" : ""}`}>{error}</p>}
            {client ? (
              route ? (
                <>
                  {/* Keep the previous page mounted so its drafts, terminal and nested scroll survive Settings. */}
                  <div hidden={settingsFrom !== null} className={settingsFrom ? "hidden" : `relative flex min-h-0 flex-1 flex-col ${shown.screen === "inbox" ? "gap-s2 p-s3" : ""}`}>
                    <Screen route={settingsFrom?.route ?? route} visit={settingsFrom?.visit ?? visit} navigate={navigate} client={client} refresh={refresh ?? noop} store={store} openUrl={openUrl} pickFolder={pickFolder} installCli={installCli} appUpdater={appUpdater} appUpdates={appUpdates} releaseBuildUpdater={releaseBuildUpdater} readAbout={readAbout} onLibraryCounts={noteLibraryCounts} />
                  </div>
                  {settingsFrom && <Screen route={route} visit={visit} navigate={navigate} client={client} refresh={refresh ?? noop} store={store} openUrl={openUrl} pickFolder={pickFolder} installCli={installCli} appUpdater={appUpdater} appUpdates={appUpdates} releaseBuildUpdater={releaseBuildUpdater} readAbout={readAbout} onLibraryCounts={noteLibraryCounts} />}
                </>
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
        <ReconnectOverlay phase={reconnect} focusBefore={focusBefore} onRetry={check} />
      </div>
      </AppCovered.Provider>
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
      <PanesGate client={client} refresh={props.refresh}>
        <Shell {...props} initialRoute={(gate.kind === "app" && gate.route) || props.initialRoute} firstRun={gate.kind === "app" && gate.fresh} />
      </PanesGate>
    </ProvidersProvider>
  );
}

const STARTUP_WAIT = 30000;
const STARTUP_CHECK = 500;

/* `only`: just those agents (one session's own), else every agent in every session. */
function waitingForPanes(workspaces: WorkspaceDTO[], only?: Set<string>): boolean {
  return workspaces.flatMap((ws) => ws.agents ?? []).some((a) =>
    (!only || only.has(a.id)) && a.provider !== "subagent" && !a.headless && !a.locked
    && !["paused", "done", "failed", "closed", "rewound", "exited"].includes(a.status) && !a.window);
}

/* Whether agents' panes may be shown yet (store/panes.ts): not until the
   engine's recovery has settled and the store holds rows read after it.
   Nothing else waits on this: the shell shows at once, from what the store
   and the sessions feed already have (a launch whose engine spends twenty
   seconds bringing sessions back used to sit behind "Starting" for all of
   them). Asked again whenever the event stream comes back after a drop: a
   new daemon (another pid, or one resuming) hides the panes until it has
   settled in turn; a blip of the same one leaves them be. The deadline
   bounds a hung read. */
function usePanesReady(client: DaemonClient | null, refresh?: () => Promise<void>): PanesReady {
  const store = useStore();
  const { reload } = useSessions();
  const [panes, setPanes] = useState<PanesReady>({ ready: !client, epoch: 0 });
  // Rounds of asking: one at the start, one more each time the stream reopens after a drop.
  const connection = useAppState((s) => s.connection);
  const [round, setRound] = useState(0);
  const dropped = useRef(false);
  useEffect(() => {
    if (connection === "closed") dropped.current = true;
    else if (connection === "open" && dropped.current) {
      dropped.current = false;
      setRound((n) => n + 1);
    }
  }, [connection]);
  // The daemon the panes were last shown from.
  const shownFrom = useRef<number | null>(null);
  // The sessions released one by one while the rest come back, and the daemon they were released by.
  const backNow = useRef<Set<string>>(new Set());
  const backPid = useRef<number | null>(null);
  // Sticky across probes and stream drops: a later calm meta response alone
  // cannot make sync's pre-recovery rows safe. Clear only after the barrier.
  const recovering = useRef(false);
  useEffect(() => {
    if (!client) return;
    let current = true;
    let first = true;
    let next: ReturnType<typeof setTimeout>;
    const settle = (pid: number | null) => {
      startupStep("panes_ready");
      current = false;
      clearTimeout(next);
      clearTimeout(poll);
      clearTimeout(deadline);
      if (pid !== null) shownFrom.current = pid;
      // `back` stays: a session released ahead of the rest already has its fresh view.
      setPanes((p) => (p.ready ? p : { ready: true, epoch: p.epoch + 1, recovery: null, back: p.back }));
    };
    // Per session (an engine that names the sessions still to come back): each one whose own agents have
    // windows in rows read after it came back is released at once, whatever the others are doing.
    const releaseBack = async (meta: Meta, pending: string[], seq: number, workspaces: WorkspaceDTO[], unchanged: () => boolean) => {
      const fresh = (rows: WorkspaceDTO[], not: string[]) => rows.flatMap((w) => w.agents ?? []).filter((a) => !a.parent_id).map((a) => a.id)
        .filter((id) => !not.includes(id) && !backNow.current.has(id) && !waitingForPanes(rows, agentIds(sessionTree(rows, id))));
      // Only those back when these rows were read: one that came back since needs a read of its own.
      const eligible = fresh(workspaces, pending);
      if (!eligible.length) return;
      // These rows were read after the meta that left them out of pending, so they are the session's
      // own: apply them as they are (as the settled path does) rather than wait for a slow store
      // refresh, which only follows in the background to catch the inbox and anything newer.
      // (Unless a newer gate read has landed: its rows are later still.)
      apply(seq, workspaces);
      // Still the same daemon, and none of these sent back to pending since. A recovery that ended
      // meanwhile has every session back (on a restart the last ones follow the viewed one closely):
      // these rows are still the session's own, so throwing them away would only wait out another read.
      const latest = await client.meta();
      const stillPending = latest.resuming ? latest.recovery?.pending : [];
      if (!unchanged() || latest.pid !== meta.pid || !stillPending) return;
      // Not stale, though: while sessions come back the stream rarely stops marking it so (each one says it is
      // starting), and the refresh that clears it is behind them. What counts is the rows applied now: those
      // read above, or a later read (sync drops a read older than them), still giving the session's own windows.
      const applied = store.getState();
      if (applied.error) return;
      const ready = fresh(applied.workspaces, stillPending).filter((id) => eligible.includes(id));
      void Promise.all([reload(), refresh?.()]).catch(() => {});
      for (const id of ready) backNow.current.add(id);
      if (ready.length) setPanes((p) => ({ ...p, back: [...backNow.current] }));
    };
    const hide = () => setPanes((p) => (p.ready ? { ...p, ready: false } : p));
    const deadline = setTimeout(() => settle(null), STARTUP_WAIT);
    // Whether a session the engine named pending at `then` is back by `now` (or `now` is another daemon):
    // a read begun at `then` cannot show it. One that ends recovery with none newly back changes nothing.
    const cameBack = (then: Meta, now: Meta) => now.pid !== then.pid
      || (then.resuming && (then.recovery?.pending ?? []).some((id) => !now.resuming || !now.recovery?.pending?.includes(id)));
    let poll: ReturnType<typeof setTimeout> | undefined;
    /* A snapshot can take seconds while sessions launch (a cold git status per worktree), so while
       the gate's reads are out the engine keeps being asked: a session that comes back meanwhile gets
       a read of its own at once (an extra pass of `check`), not after the reads out end and another
       after that. The reads already out still count for the sessions back when they began. */
    // The engine's word the newest read began under (or the newest an extra pass was asked for).
    let asked: Meta | null = null;
    let out = 0;
    let watching = false;
    const watch = async () => {
      if (watching) return;
      watching = true;
      try {
        while (current && out > 0) {
          await new Promise((ok) => { poll = setTimeout(ok, STARTUP_CHECK); });
          if (!current || out === 0) break;
          const now = await client.meta().catch(() => null);
          if (current && now && asked && cameBack(asked, now)) {
            asked = now;
            void check(true);
          }
        }
      } finally {
        watching = false;
      }
    };
    // Each read's place among all snapshot reads, sync's too (snapshotTicket): the store keeps
    // the rows of the newest begun, so an older read that ends later never puts its rows over them.
    const readAfter = async (meta: Meta) => {
      const seq = snapshotTicket();
      asked = meta;
      out++;
      void watch();
      try {
        return { seq, workspaces: await client.snapshot() };
      } finally {
        out--;
      }
    };
    const apply = (seq: number, workspaces: WorkspaceDTO[]) => store.dispatch({ type: "snapshot", workspaces, began: seq });
    const check = async (extra = false) => {
      const { connectionGeneration, lifecycleVersion } = store.getState();
      // A read from before the stream dropped and came back may be the old daemon's.
      // A close/rewind also invalidates rows, even on an uninterrupted stream.
      const unchanged = () => {
        const state = store.getState();
        return current && state.connectionGeneration === connectionGeneration && state.lifecycleVersion === lifecycleVersion;
      };
      try {
        const meta = await client.meta();
        if (!unchanged()) return;
        if (first && round > 0 && meta.pid === shownFrom.current && !meta.resuming && !recovering.current) return settle(meta.pid);
        // Recovery may finish between sync starting and our first meta read.
        // A replacement daemon also needs the barrier even if already settled.
        if (meta.resuming || (meta.recovered_at ?? 0) >= client.createdAt
          || (round > 0 && meta.pid !== shownFrom.current)) recovering.current = true;
        first = false;
        hide();
        const recovery = meta.resuming ? meta.recovery ?? null : null;
        const pending = meta.resuming ? recovery?.pending : undefined;
        // Another daemon: whatever was back with the last one is not known to be back with this.
        if (backPid.current !== meta.pid) {
          backPid.current = meta.pid;
          backNow.current = new Set();
        }
        const back = [...backNow.current];
        setPanes((p) => (p.recovery?.done === recovery?.done && p.recovery?.total === recovery?.total && p.pending?.join() === pending?.join() && p.back?.join() === back.join()
          ? p : { ...p, recovery, pending: pending ?? null, back }));
        if (pending) {
          const { seq, workspaces } = await readAfter(meta);
          if (!unchanged()) return;
          await releaseBack(meta, pending, seq, workspaces, unchanged);
        }
        if (!meta.resuming) {
          const { seq, workspaces } = await readAfter(meta);
          if (!unchanged()) return;
          if (!waitingForPanes(workspaces)) {
            if (recovering.current) {
              // These rows were read after recovery ended. Validate their daemon
              // before applying them, independently of sync's inbox and queued reads.
              const latest = await client.meta();
              if (!unchanged() || latest.resuming || latest.pid !== meta.pid) return;
            }
            // Applying this read also supersedes sync's in-flight pre-recovery rows.
            apply(seq, workspaces);
            // Unless a read begun after it has landed first: the store kept those rows, and they decide.
            if (waitingForPanes(store.getState().workspaces)) return;
            recovering.current = false;
            settle(meta.pid);
            void Promise.all([reload(), refresh?.()]).catch(() => {});
            return;
          }
        }
      } catch {
        // Not answering yet: ask again shortly.
      } finally {
        // An extra pass (a session back while reads were out) is one look: the loop goes on as it was.
        if (current && !extra) next = setTimeout(() => void check(), STARTUP_CHECK);
      }
    };
    void check();
    return () => {
      current = false;
      clearTimeout(next);
      clearTimeout(poll);
      clearTimeout(deadline);
    };
  }, [client, round, reload, refresh, store]);
  return panes;
}

function PanesGate({ client, refresh, children }: { client: DaemonClient | null; refresh?: () => Promise<void>; children: ReactNode }) {
  const panes = usePanesReady(client, refresh);
  return <PanesReadyContext.Provider value={panes}>{children}</PanesReadyContext.Provider>;
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
