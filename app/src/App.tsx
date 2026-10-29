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
   agent), Settings (keys, the repo's settings, permission
   rules, doctor, the engine's update) and Account (the plan, sign-in,
   spend). A provider's sign-in runs on the engine with a card that follows
   it (components/ProviderSignIn.tsx), and the engine's upgrade as a
   progress card (components/EngineUpgrade.tsx); the one terminal that is the
   person's own is the Terminal docked under the main column, a plain shell
   in the open repo (components/TerminalPanel.tsx, shown from the sidebar's
   status line or ⌃`). The engine banner sits above
   the screen when the engine isn't one the app can simply use, or its
   event stream is down (components/EngineBanner.tsx). Before all of it, on
   a first launch, the full-window flow that connects the first provider
   (screens/Onboarding: no sidebar), until one is connected and signed in. */

import { useCallback, useEffect, useMemo, useState } from "react";
import { DaemonError, type DaemonClient, type ProviderInstalled } from "./api/client";
import { plain } from "./api/wording";
import { DaemonContext } from "./api/context";
import { checkEngine, type EngineStatus } from "./api/engine";
import { openExternal, pickFolder as nativePicker, type OpenUrl, type PickFolder } from "./api/open";
import type { SessionDTO } from "./api/schema";
import type { About, AppUpdater, AppUpdates, CliLink, ReleaseBuildUpdater } from "./api/tauri";
import { UpgradeCard, useEngineUpgrade } from "./components/EngineUpgrade";
import { EngineBanner } from "./components/EngineBanner";
import { Sidebar } from "./components/Sidebar";
import { TerminalPanel, TerminalProvider, shellPlace } from "./components/TerminalPanel";
import { alert, button } from "./components/ui";
import { HOME, readLastSession, writeLastSession, type Route, type SessionRef, type SheetRef } from "./routes";
import { AccountScreen } from "./screens/Account";
import { InboxScreen } from "./screens/Inbox";
import { KnowledgeScreen } from "./screens/Knowledge";
import { NewSession } from "./screens/New";
import { Onboarding, Starting, markOnboarded, onboarded, readOnboarded } from "./screens/Onboarding";
import { AgentsScreen } from "./screens/Agents";
import { SessionScreen } from "./screens/Session";
import { SettingsScreen } from "./screens/Settings";
import { isPaused } from "./store/overview";
import { SessionsProvider, useCloseSession, useSessions } from "./store/sessions";
import { ProvidersProvider } from "./store/providers";
import { UsageProvider } from "./store/usage";
import type { AppState, AppStore } from "./store/store";
import { StoreContext, useAppState } from "./store/useStore";

export interface AppProps {
  store: AppStore;
  /* The engine; null when the app has none to talk to (a plain browser tab
     without VITE_BRINDLE_DAEMON_URL). */
  client: DaemonClient | null;
  /* Fetch the snapshot and inbox again (Sync.refresh). */
  refresh?: () => Promise<void>;
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

function Screen({ route, navigate, client, refresh, store, openUrl, pickFolder, installCli, appUpdater, appUpdates, releaseBuildUpdater, readAbout }: {
  route: Route;
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
  switch (route.screen) {
    case "knowledge":
      return <KnowledgeScreen client={client} pickFolder={pickFolder} />;
    case "agents":
      return <AgentsScreen client={client} onNavigate={(to) => navigate(to === "providers" ? { screen: "settings", section: "providers", ask: ++providerAsks } : { screen: "knowledge" })} />;
    case "settings":
      return <SettingsScreen client={client} installCli={installCli} appUpdater={appUpdater} appUpdates={appUpdates} releaseBuildUpdater={releaseBuildUpdater} readAbout={readAbout} section={route.section} ask={route.ask} rule={route.rule} current={readLastSession()?.repo} />;
    case "project-settings":
      return <SettingsScreen key={`repo:${route.repo}`} client={client} installCli={installCli} appUpdater={appUpdater} appUpdates={appUpdates} releaseBuildUpdater={releaseBuildUpdater} readAbout={readAbout} repo={route.repo} />;
    case "account":
      return <AccountScreen client={client} openUrl={openUrl} onNavigate={(screen) => navigate({ screen })} section={route.section} ask={route.ask} />;
    case "new":
      return <NewSession key={route.repo} client={client} onOpen={openSession} initialRepo={route.repo} pickFolder={pickFolder} refresh={refresh} />;
    case "inbox":
      return <InboxScreen client={client} refresh={refresh} onOpen={openSession} session={route.session ?? null} onShowAll={() => navigate({ screen: "inbox" })} onOpenRule={(rule) => navigate({ screen: "settings", section: "permissions", rule, ask: ++providerAsks })} appUpdates={appUpdates} openUrl={openUrl} />;
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

function Shell({ store, client, refresh, initialRoute, openUrl = openExternal, pickFolder = nativePicker, installCli, appUpdater, appUpdates, releaseBuildUpdater, readAbout }: AppProps) {
  // Until the sessions are known the app shows the empty state, quietly; then it lands.
  const [route, setRoute] = useState<Route | null>(initialRoute ?? null);
  const navigate = useCallback((to: Route) => setRoute(to), []);
  const { repos, sessions, error: feedError, reload } = useSessions();
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
  // Where a shell of the person's own starts: the open session's worktree, else the repo's root.
  const workspaces = useAppState((s) => s.workspaces);
  const here = useMemo(() => shellPlace(shown, repos ?? [], workspaces, shown.screen === "session" ? sessions[shown.repo]?.find((s) => s.id === shown.root) : null), [shown, repos, workspaces, sessions]);
  return (
    <TerminalProvider client={client} here={here}>
      <div className="flex h-full bg-canvas text-ink">
        <Sidebar route={shown} navigate={navigate} refresh={refresh ?? noop} openUrl={openUrl} />
        <div className="relative flex min-w-0 flex-1 flex-col">
          {/* The banner's upgrade follows the engine's terminal stream, which a page with no engine doesn't have. */}
          {client ? <Banner status={bannerStatus(engine, connection)} connection={connection} onRetry={check} client={client} store={store} /> : <EngineBanner status={bannerStatus(engine, connection)} connection={connection} onRetry={check} />}
          <main className={`relative flex min-h-0 flex-1 flex-col ${fills ? "overflow-hidden" : "gap-s2 overflow-auto p-s3"}`}>
            {error && <p className={`${alert} ${fills ? "m-s2 mb-0" : ""}`}>{error}</p>}
            {client ? (
              route ? (
                <div className={`relative flex min-h-0 flex-1 flex-col ${shown.screen === "inbox" ? "gap-s2 p-s3" : ""}`}>
                  <Screen route={route} navigate={navigate} client={client} refresh={refresh ?? noop} store={store} openUrl={openUrl} pickFolder={pickFolder} installCli={installCli} appUpdater={appUpdater} appUpdates={appUpdates} releaseBuildUpdater={releaseBuildUpdater} readAbout={readAbout} />
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
      </div>
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
type Gate = { kind: "checking"; error?: string } | { kind: "onboarding"; providers: ProviderInstalled[] } | { kind: "app"; route?: Route };

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
        markOnboarded();
        setGate({ kind: "app" });
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
  const done = useCallback((route?: Route) => setGate({ kind: "app", route }), []);
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
  // The providers feed starts with the app itself: the first launch reads its own rows.
  return (
    <ProvidersProvider client={client}>
      <Shell {...props} initialRoute={(gate.kind === "app" && gate.route) || props.initialRoute} />
    </ProvidersProvider>
  );
}

export default function App(props: AppProps) {
  return (
    <StoreContext.Provider value={props.store}>
      <DaemonContext.Provider value={props.client}>
        <SessionsProvider client={props.client}>
          <UsageProvider client={props.client}>
            <FirstLaunch {...props} />
          </UsageProvider>
        </SessionsProvider>
      </DaemonContext.Provider>
    </StoreContext.Provider>
  );
}
