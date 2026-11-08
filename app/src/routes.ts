/* Where the app is: one screen and what it shows. A session's view (the
   provider's own session, front and centre) carries the session's root
   agent id and its repo, as the sessions feed gave them (the goal is asked
   for with the repo, and a paused session's root is in no snapshot), plus
   which of its agents is selected and which sheet, if any, lies over its
   surface (an inbox item's). "new" is the calm
   empty state with the New session composer (where the app opens with no
   session to show); the Inbox lists everything waiting on a person, each
   row jumping to its session. Knowledge (a repo's decisions and sources),
   Agents (which model runs each kind of work, and the person's custom
   agents) and Settings (with Account and Usage among its tabs) are the pages behind the profile button's rail. There is
   no home board: the sidebar is the overview. */

export interface SessionRef {
  /* The session's root agent id. */
  root: string;
  /* The repo it belongs to (its root path). */
  repo: string;
}

/* What is open over the selected agent's surface: the session's "Review &
   open PR" (screens/Session/ReviewSheet), its one deliberate entry, once
   the engine says the session is ready for one. Nothing else ever lies
   over a session: what waits on the person is acted on from the Inbox's
   cards or in the agent's own terminal. */
/* What lies over a session's chat: the review sheet (an open PR's Review changes), the session's
   changes in the chat's place (the sidebar's Changes row), or one of its visuals in the chat's
   place (the header's Visuals, a brindle://visual/<id> link in the chat). */
export type SheetRef = { kind: "review" } | { kind: "changes" } | { kind: "visual"; id: string };

/* Knowledge's tabs. */
export type KnowledgeView = "map" | "docs" | "imported" | "decisions" | "kept";

export type Route =
  /* `repo`: the project to start in (one just picked, as the first launch ends). */
  | { screen: "new"; repo?: string }
  /* `session`: only what waits in that session (its root), from its bar's count. */
  | { screen: "inbox"; session?: string }
  /* `doc`: a library doc's id (opened in the Docs reader) or an imported doc's number;
     `session`: Docs only of that session (its root), from the session's goal panel;
     `from`: the session the person came from, for Docs' Back to session. */
  | { screen: "knowledge"; view?: KnowledgeView; doc?: number | string; section?: string; session?: string; from?: SessionRef }
  | { screen: "agents" }
  /* Settings, one place with tabs (settingsTab below). `tab`: the tab to open on;
     `section`: the tab it lives on, scrolled to on opening (a provider's problem opens
     Settings › Providers); `ask` counts the requests, so asking again while the page is
     open switches and scrolls again; `rule`: a permission rule to open in its editor,
     from the Inbox's permission items. */
  | { screen: "settings"; tab?: SettingsTab; section?: "providers" | "permissions" | "storage"; ask?: number; rule?: string; repo?: string }
  /* The older routes, kept: they open Settings on their tab. One repo's checks, rules and
     overrides of the person's settings (from its sidebar heading): the Project tab. */
  | { screen: "project-settings"; repo?: string }
  /* The plan, sign-in and spend (the sidebar's initials button): the Account tab; `section`
     "usage" (the sidebar's usage reading) the Usage tab. */
  | { screen: "account"; section?: "usage"; ask?: number }
  | ({
      screen: "session";
      agent?: string;
      sheet?: SheetRef;
    } & SessionRef);

/* Settings' tabs, in order: everything the person sets, behind the sidebar's account entry. */
export const SETTINGS_TABS = [
  { id: "account", label: "Account" },
  { id: "usage", label: "Usage" },
  { id: "general", label: "General" },
  { id: "providers", label: "Workers & providers" },
  { id: "permissions", label: "Permissions" },
  { id: "storage", label: "Storage" },
  { id: "project", label: "Project" },
  { id: "about", label: "About" },
] as const;
export type SettingsTab = (typeof SETTINGS_TABS)[number]["id"];

/* The Settings tab a route opens on, the older Account and Project settings routes
   included; null for a route that isn't Settings. */
export function settingsTab(route: Route): SettingsTab | null {
  switch (route.screen) {
    case "account":
      return route.section === "usage" ? "usage" : "account";
    case "project-settings":
      return "project";
    case "settings":
      return route.tab ?? route.section ?? "general";
    default:
      return null;
  }
}

/* The full screens the profile button's rail also opens, beside Settings' tabs. */
export type RailPage = "knowledge" | "agents";
export function railPage(route: Route): RailPage | null {
  return route.screen === "knowledge" || route.screen === "agents" ? route.screen : null;
}

/* Whether the sidebar shows the profile rail (with its "← Sessions") and Esc goes back. */
export const inProfileRail = (route: Route): boolean => settingsTab(route) !== null || railPage(route) !== null;

/* Where the app lands before it knows its sessions: the empty state. */
export const HOME: Route = { screen: "new" };

/* The session a route belongs to, if any. */
export const routeSession = (route: Route): SessionRef | null =>
  "root" in route ? { root: route.root, repo: route.repo } : null;

/* The same place in the app: the same screen, and for a session, the same session. */
export const sameRoute = (a: Route, b: Route): boolean =>
  a.screen === b.screen && routeSession(a)?.root === routeSession(b)?.root;

/* The session the app opened last, kept so the next launch opens on it. */
export const LAST_SESSION_KEY = "brindle.session.last";

export function readLastSession(): SessionRef | null {
  try {
    const raw = localStorage.getItem(LAST_SESSION_KEY);
    if (!raw) return null;
    const got = JSON.parse(raw) as Partial<SessionRef>;
    return typeof got.root === "string" && typeof got.repo === "string" ? { root: got.root, repo: got.repo } : null;
  } catch {
    return null;
  }
}

export function writeLastSession(ref: SessionRef): void {
  try {
    localStorage.setItem(LAST_SESSION_KEY, JSON.stringify(ref));
  } catch {
    // No storage: the next launch opens on the first live session.
  }
}
