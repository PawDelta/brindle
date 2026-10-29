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
   agents), Settings and Account are the pages behind the sidebar's foot. There is
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
/* What lies over a session's chat: the review sheet (an open PR's Review changes), or the session's
   changes in the chat's place (the sidebar's Changes row). */
export type SheetRef = { kind: "review" } | { kind: "changes" };

export type Route =
  /* `repo`: the project to start in (one just picked, as the first launch ends). */
  | { screen: "new"; repo?: string }
  /* `session`: only what waits in that session (its root), from its bar's count. */
  | { screen: "inbox"; session?: string }
  | { screen: "knowledge" }
  | { screen: "agents" }
  /* `section`: scrolled to on opening (the sidebar's usage reading opens Account › Usage,
     a provider's problem Settings › Providers); `ask` counts the requests, so asking
     again while the page is open scrolls again; `rule`: a permission rule to open in its
     editor, from the Inbox's permission items. */
  | { screen: "settings"; section?: "providers" | "permissions"; ask?: number; rule?: string }
  | { screen: "account"; section?: "usage"; ask?: number }
  | ({ screen: "session"; agent?: string; sheet?: SheetRef } & SessionRef);

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
