import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { isTauri } from "@tauri-apps/api/core";
import App from "./App";
import { DaemonClient } from "./api/client";
import { watchDebug } from "./api/debug";
import { AppPtyViews } from "./api/ptyView";
import { tauriSetup } from "./api/setup";
import { appUpdater, appUpdates, installCli, readAbout, releaseBuildUpdater, tauriClient, tauriSocket } from "./api/tauri";
import { applyTheme, readTheme } from "./components/ThemeToggle";
import type { Route } from "./routes";
import { createAppStore } from "./store/store";
import { startSync, type Sync } from "./store/sync";
import { Launch } from "./screens/FirstRun";
import { tauriSink, watchInbox } from "./screens/Inbox";
import "./styles.css";

// The theme the person picked last time, before the first paint.
applyTheme(readTheme());

const store = createAppStore();

// Notifications and the tray badge belong to the shell; a plain browser tab has neither.
if (isTauri()) watchInbox(store, tauriSink());

// In the app the shell finds (or starts) the daemon and carries every request
// to it, token included (src-tauri/src/daemon.rs). `npm run dev` in a browser
// can be pointed at one by env instead: VITE_BRINDLE_DAEMON_URL and _TOKEN,
// which win when set. Never in a release build, which must not carry a token.
const { DEV, VITE_BRINDLE_DAEMON_URL: baseUrl, VITE_BRINDLE_DAEMON_TOKEN: token } = import.meta.env;
const devClient = DEV && baseUrl && token ? new DaemonClient({ baseUrl, token }) : null;
const client = devClient ?? (isTauri() ? tauriClient() : null);
const connect = devClient ? undefined : tauriSocket;

// `npm run dev` can open on a session: VITE_BRINDLE_REPO (its repo's root
// path) and VITE_BRINDLE_ROOT (its root agent), with the daemon above.
const { VITE_BRINDLE_REPO: repo, VITE_BRINDLE_ROOT: root } = import.meta.env;
const initialRoute: Route | undefined = client && repo && root ? { screen: "session", root, repo } : undefined;

// The shell's setup: the first run's install (the engine's own update runs on the engine, in Settings),
// and the command-line tool's link (Settings' "Install command-line tool"), which only the shell can make.
const setup = client && !devClient ? tauriSetup() : undefined;
const cli = setup ? installCli : undefined;
const updater = setup ? appUpdater : undefined;
const releaseUpdater = setup ? releaseBuildUpdater : undefined;
// Settings' About reads the bundle's license files through the shell, whichever daemon the app talks to.
const about = isTauri() ? readAbout : undefined;
// The app's own updates (the Inbox's Restart to update, About's Check now): the shell's, whichever daemon.
const updates = isTauri() ? appUpdates : undefined;

// The store syncs from the moment the app shows, once; and the app asks
// then whether staff debug logging is on (api/debug.ts).
let sync: Sync | null = null;
function app() {
  if (client && !sync) {
    sync = startSync({ client, store, connect });
    watchDebug(client);
  }
  return <App store={store} client={client} refresh={sync?.refresh} initialRoute={initialRoute} installCli={cli} appUpdater={updater} appUpdates={updates} releaseBuildUpdater={releaseUpdater} readAbout={about} />;
}

// In the app, the daemon is found (or started) first; when it can't be, the
// first run's setup shows instead (screens/FirstRun).
// Panes show through a real tmux client on a pty of the shell's (api/ptyView.ts).
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AppPtyViews>{setup ? <Launch setup={setup}>{app}</Launch> : app()}</AppPtyViews>
  </StrictMode>,
);
