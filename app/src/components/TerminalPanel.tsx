/* The Terminal: a plain shell of the person's own in the open repo, docked
   at the foot of the main column, under the session (design/terminal,
   option A). Closed, only the sidebar status line's ">_ Terminal ⌃`"
   (TerminalToggle) says it is there; ⌃` does the same. Open, a grip above
   it resizes it (its height kept per repo, about a third to start), and
   its bar is the tab strip: one shell per repo named by the repo, + for
   another in the same repo, then where the shell started, in words and as
   the path, then collapse and close. Nothing lies over the session: the
   session's screen gets shorter.

   A shell is the daemon's (POST /terminals/shell {cwd}: the person's login
   shell in a window of its own tmux session), started in the open
   session's agent's worktree, else the repo's root, and shown as any pane
   is, through PtyTerminal. Collapsing keeps the shells running (their
   views close and attach again on opening); × closes the shown one
   (DELETE /terminals/{id}), its tab kept until the daemon says it's gone,
   and a shell that exits leaves its tab. A start or close that fails says
   so under the bar, and a shell that can't be shown (no pty view here, or
   its view couldn't attach) says why in its place, with a way to try again. */

import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef, useState, type MouseEvent as ReactMouseEvent, type ReactNode } from "react";
import { DaemonError, type DaemonClient } from "../api/client";
import { usePtyViews, type PtyViewTarget } from "../api/ptyView";
import type { WorkspaceDTO } from "../api/schema";
import { plain } from "../api/wording";
import { readLastSession, type Route } from "../routes";
import { PtyTerminal } from "./PtyTerminal";

/* Where a shell opened now would start: the repo (its root and name), the
   folder, and that folder in words. */
export interface ShellPlace {
  repo: string;
  name: string;
  cwd: string;
  why: string;
}

/* The open session's agent's worktree, else the root of the repo on screen
   (a session's, else the last session's, else the first repo). */
export function shellPlace(route: Route, repos: { repo_root: string; name: string }[], workspaces: WorkspaceDTO[]): ShellPlace | null {
  const repo = route.screen === "session" ? route.repo : (route.screen === "new" && route.repo) || readLastSession()?.repo || repos[0]?.repo_root;
  if (!repo) return null;
  const name = repos.find((r) => r.repo_root === repo)?.name ?? repo.split("/").filter(Boolean).pop() ?? repo;
  if (route.screen === "session") {
    const agent = route.agent ?? route.root;
    const ws = workspaces.find((w) => w.agents?.some((a) => a.id === agent));
    if (ws && ws.path !== repo) return { repo, name, cwd: ws.path, why: `started in ${agent}'s worktree` };
  }
  return { repo, name, cwd: repo, why: "started at the repo root" };
}

/* A home folder as ~, the way a shell's prompt shows it. */
export const tilde = (path: string) => path.replace(/^\/(?:Users|home)\/[^/]+(?=\/|$)/, "~");

export interface ShellTab extends ShellPlace {
  terminal: string;
  at: PtyViewTarget;
  /* The shell's program as the daemon names it ("zsh"); "shell" where it doesn't. */
  shell: string;
}

/* The panel's height as a share of the column, per repo. */
export const HEIGHT_KEY = "brindle.terminal.height";
const HEIGHT_DEFAULT = 35;
const HEIGHT_MIN = 15;
const HEIGHT_MAX = 80;
const clampHeight = (n: number) => Math.min(HEIGHT_MAX, Math.max(HEIGHT_MIN, n));

function readHeights(): Record<string, number> {
  try {
    const got: unknown = JSON.parse(localStorage.getItem(HEIGHT_KEY) ?? "{}");
    return got && typeof got === "object" ? (got as Record<string, number>) : {};
  } catch {
    return {};
  }
}

function writeHeights(heights: Record<string, number>): void {
  try {
    localStorage.setItem(HEIGHT_KEY, JSON.stringify(heights));
  } catch {
    // No storage: the height lasts for this window.
  }
}

interface Terminals {
  open: boolean;
  /* Show the panel (a shell started for the repo on screen if it has none), or collapse it. */
  toggle(): void;
  collapse(): void;
  /* Another shell where the open session's would start. */
  add(): void;
  close(terminal: string): Promise<void>;
  select(terminal: string): void;
  /* The shell's view ended (it exited, or its window went): its tab goes. */
  ended(terminal: string): void;
  tabs: ShellTab[];
  active: ShellTab | null;
  here: ShellPlace | null;
  /* A shell is being started (POST /terminals/shell is out). */
  starting: boolean;
  /* Why the last start or close failed, until the next one. */
  error: string | null;
  height: number;
  resize(height: number): void;
}

const TerminalContext = createContext<Terminals | null>(null);

/* The panel's shells, which one is shown, and whether it is open: one state, so a
   shell started and another closed in the same moment both land, in either order. */
interface Dock {
  open: boolean;
  tabs: ShellTab[];
  active: string | null;
  /* A shell is being started (POST /terminals/shell is out): the panel stays open for it. */
  starting: boolean;
}

type DockAction =
  | { kind: "open"; open: boolean }
  | { kind: "starting" }
  | { kind: "failed" }
  | { kind: "added"; tab: ShellTab }
  | { kind: "dropped"; terminal: string }
  | { kind: "select"; terminal: string }
  /* The repo on screen changed, or the panel opened: its shell is shown, where it has one. */
  | { kind: "follow"; repo: string | undefined };

function dock(state: Dock, action: DockAction): Dock {
  switch (action.kind) {
    case "open":
      return state.open === action.open ? state : { ...state, open: action.open };
    case "starting":
      return { ...state, starting: true };
    case "failed":
      return { ...state, starting: false };
    case "added":
      return { ...state, starting: false, tabs: [...state.tabs.filter((t) => t.terminal !== action.tab.terminal), action.tab], active: action.tab.terminal };
    case "dropped": {
      // The tab goes; the one beside it is shown, and with none left (and none on its way) the panel collapses.
      const at = state.tabs.findIndex((t) => t.terminal === action.terminal);
      if (at < 0) return state;
      const tabs = state.tabs.filter((t) => t.terminal !== action.terminal);
      const active = state.active === action.terminal ? (tabs[Math.min(at, tabs.length - 1)]?.terminal ?? null) : state.active;
      return { ...state, open: state.open && (tabs.length > 0 || state.starting), tabs, active };
    }
    case "select":
      return { ...state, active: action.terminal };
    case "follow": {
      if (state.tabs.find((t) => t.terminal === state.active)?.repo === action.repo) return state;
      const mine = state.tabs.find((t) => t.repo === action.repo);
      return mine ? { ...state, active: mine.terminal } : state;
    }
  }
}

export function TerminalProvider({ client, here, children }: { client: DaemonClient | null; here: ShellPlace | null; children?: ReactNode }) {
  const [{ open, tabs, active: activeId, starting }, dispatch] = useReducer(dock, { open: false, tabs: [], active: null, starting: false });
  const [error, setError] = useState<string | null>(null);
  const [heights, setHeights] = useState(readHeights);
  const busy = useRef(false);

  const start = useCallback(
    async (place: ShellPlace) => {
      if (!client || busy.current) return;
      busy.current = true;
      dispatch({ kind: "starting" });
      setError(null);
      try {
        const got = await client.openShell(place.cwd);
        const tab: ShellTab = { ...place, terminal: got.terminal, at: { server: got.server ?? null, target: got.target }, shell: got.shell || "shell" };
        dispatch({ kind: "added", tab });
      } catch (e) {
        // The shell shown stays as it was; the request's failure says so beside it.
        dispatch({ kind: "failed" });
        setError(`Couldn't start a shell: ${plain(e instanceof Error ? e.message : String(e))}`);
      } finally {
        busy.current = false;
      }
    },
    [client],
  );

  // On switching repo, the tab shown follows: that repo's shell, where it has one.
  const repo = here?.repo;
  useEffect(() => dispatch({ kind: "follow", repo }), [repo]);

  const toggle = useCallback(() => {
    if (open) return dispatch({ kind: "open", open: false });
    dispatch({ kind: "open", open: true });
    if (tabs.some((t) => t.repo === here?.repo)) dispatch({ kind: "follow", repo: here?.repo });
    else if (here) void start(here);
  }, [open, tabs, here, start]);

  const drop = useCallback((terminal: string) => dispatch({ kind: "dropped", terminal }), []);

  // The tab stays until the daemon says the shell is gone: a close that fails says why, and × tries again.
  const close = useCallback(
    async (terminal: string) => {
      if (!client) return;
      setError(null);
      try {
        await client.closeTerminal(terminal);
        drop(terminal);
      } catch (e) {
        // Gone already (it exited, or the engine restarted without it): nothing is left to close.
        if (e instanceof DaemonError && e.code === "not_found") drop(terminal);
        else setError(`Couldn't close the shell: ${plain(e instanceof Error ? e.message : String(e))}`);
      }
    },
    [client, drop],
  );

  const resize = useCallback(
    (next: number) => {
      if (!repo) return;
      setHeights((all) => {
        const updated = { ...all, [repo]: clampHeight(next) };
        writeHeights(updated);
        return updated;
      });
    },
    [repo],
  );

  // ⌃` shows or hides it, wherever the focus is (a terminal's own too).
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (!e.ctrlKey || e.metaKey || e.altKey || (e.key !== "`" && e.code !== "Backquote")) return;
      e.preventDefault();
      e.stopPropagation();
      toggle();
    };
    window.addEventListener("keydown", key, true);
    return () => window.removeEventListener("keydown", key, true);
  }, [toggle]);

  const value = useMemo<Terminals>(
    () => ({
      open,
      toggle,
      collapse: () => dispatch({ kind: "open", open: false }),
      add: () => void (here && start(here)),
      close,
      select: (terminal) => dispatch({ kind: "select", terminal }),
      ended: drop,
      tabs,
      active: tabs.find((t) => t.terminal === activeId) ?? null,
      here,
      starting,
      error,
      height: (repo && heights[repo]) || HEIGHT_DEFAULT,
      resize,
    }),
    [open, toggle, here, start, close, drop, tabs, activeId, starting, error, repo, heights, resize],
  );
  return <TerminalContext.Provider value={value}>{children}</TerminalContext.Provider>;
}

export const useTerminals = () => useContext(TerminalContext);

/* The status line's ">_ Terminal ⌃`": lit while the panel is open. */
export function TerminalToggle() {
  const terminals = useTerminals();
  if (!terminals) return null;
  const { open, toggle, here } = terminals;
  return (
    <button
      type="button"
      className={`ml-auto inline-flex h-5 flex-none items-center gap-1.5 rounded-md border pl-1.5 pr-[7px] text-[11.5px] disabled:opacity-50 ${open ? "border-fawn bg-fawn-soft text-ink" : "border-line text-ink-3 hover:border-ink-3 hover:text-ink"}`}
      aria-pressed={open}
      disabled={!here}
      title="Show or hide the Terminal (⌃`)"
      data-testid="terminal-toggle"
      onClick={toggle}
    >
      <span className={`font-mono text-[10.5px] font-bold tracking-[-0.04em] ${open ? "text-fawn" : ""}`} aria-hidden>
        &gt;_
      </span>
      Terminal
      <span className="font-mono text-[10px] opacity-80" aria-hidden>
        ⌃`
      </span>
    </button>
  );
}

/* The grip above the panel: drag it to resize, ↑ ↓ with the keyboard. */
function Grip({ height, onChange }: { height: number; onChange(next: number): void }) {
  const [dragging, setDragging] = useState(false);
  const self = useRef<HTMLDivElement>(null);
  const down = (e: ReactMouseEvent) => {
    setDragging(true);
    e.preventDefault();
  };
  useEffect(() => {
    if (!dragging) return;
    // The column the panel takes its share of: the grip's parent.
    const column = self.current?.parentElement?.getBoundingClientRect();
    const move = (e: MouseEvent) => {
      if (column && column.height > 0) onChange(((column.bottom - e.clientY) / column.height) * 100);
    };
    const up = () => setDragging(false);
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
  }, [dragging, onChange]);
  return (
    <div
      ref={self}
      role="separator"
      aria-orientation="horizontal"
      aria-label="Resize the Terminal"
      aria-valuenow={Math.round(height)}
      aria-valuemin={HEIGHT_MIN}
      aria-valuemax={HEIGHT_MAX}
      tabIndex={0}
      title="Drag to resize"
      className="side-handle flex h-3 flex-none items-center justify-center outline-none"
      data-dragging={dragging}
      data-testid="terminal-grip"
      onMouseDown={down}
      onKeyDown={(e) => {
        if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
        e.preventDefault();
        onChange(height + (e.key === "ArrowUp" ? 5 : -5));
      }}
    >
      <i />
    </div>
  );
}

const BAR_BUTTON = "inline-grid h-[22px] w-[22px] flex-none self-center place-items-center rounded-md text-[14px] text-shell-dim hover:bg-white/5 hover:text-shell-ink";

/* The docked panel, under whatever the main column shows; nothing while collapsed. */
export function TerminalPanel() {
  const terminals = useTerminals();
  if (!terminals?.open) return null;
  const { tabs, active, here, starting, error, height, resize, add, close, collapse, select, ended } = terminals;
  return (
    <>
      <Grip height={height} onChange={resize} />
      <section
        className="mx-s2 mb-s2 flex min-h-[120px] flex-none flex-col overflow-hidden rounded-md border border-line bg-shell-bg"
        style={{ height: `${height}%` }}
        aria-label="Terminal"
        data-testid="terminal-panel"
      >
        <div className="flex h-8 flex-none items-stretch border-b border-shell-line bg-shell-bg-2 pr-1.5 text-[12px] text-shell-dim" role="tablist" aria-label="Shells">
          {tabs.map((tab) => {
            const on = tab.terminal === active?.terminal;
            return (
              <button
                key={tab.terminal}
                type="button"
                role="tab"
                aria-selected={on}
                className={`relative inline-flex items-center gap-[7px] whitespace-nowrap border-r border-shell-line px-3 ${on ? "bg-shell-bg text-shell-ink after:absolute after:inset-x-0 after:-bottom-px after:h-px after:bg-shell-bg" : "hover:text-shell-ink"}`}
                title={tab.cwd}
                onClick={() => select(tab.terminal)}
              >
                <span className={`font-mono text-[11px] font-bold tracking-[-0.04em] ${on ? "text-shell-acc" : ""}`} aria-hidden>
                  &gt;_
                </span>
                Terminal
                <span className="font-mono text-[11.5px] opacity-85">{tab.name}</span>
              </button>
            );
          })}
          <button type="button" className="border-r border-shell-line px-2.5 text-[15px] leading-none hover:text-shell-ink disabled:opacity-50" title={here ? `Another shell in ${here.name}` : "Another shell"} aria-label="Another shell" disabled={!here} onClick={add}>
            +
          </button>
          <span className="inline-flex min-w-0 flex-1 items-center gap-1.5 overflow-hidden whitespace-nowrap px-3 text-[11.5px]" data-testid="terminal-where">
            {active ? (
              <>
                <span className="text-shell-dim">
                  {active.shell}, {active.why}
                </span>
                <span className="truncate font-mono text-[11px] text-shell-ink/80">{tilde(active.cwd)}</span>
              </>
            ) : (
              starting && <span>Starting a shell…</span>
            )}
          </span>
          <button type="button" className="mr-1 flex-none self-center rounded-sm border border-shell-line px-1.5 py-px font-mono text-[10.5px] leading-[1.5] text-shell-dim hover:text-shell-ink" title="Hide the Terminal (⌃`)" aria-label="Hide the Terminal" onClick={collapse}>
            ⌃`
          </button>
          <button type="button" className={BAR_BUTTON} title="Close this shell" aria-label="Close this shell" disabled={!active} onClick={() => active && void close(active.terminal)}>
            ×
          </button>
        </div>
        {/* A start or close that failed, whichever shell is shown: that one stays as it was. */}
        {error && (
          <p role="alert" className="flex-none border-b border-shell-line bg-shell-bg-2 px-3 py-1 text-[11.5px] text-shell-bad" data-testid="terminal-error">
            {error}
          </p>
        )}
        {active && <ShellView key={active.terminal} tab={active} onEnded={() => ended(active.terminal)} />}
      </section>
    </>
  );
}

const NOTE = "flex flex-1 flex-col items-start gap-s1 p-s2 text-sm text-shell-dim";
const RETRY = "rounded-md border border-shell-line px-2 py-0.5 text-[12px] text-shell-ink hover:bg-white/5";

/* A shell's pane, once the app's pty view can show panes: until then, or
   where it can't, or when its view couldn't attach, why, with a way to try
   again (the shell's tab stays: the shell itself still runs on the daemon). */
export function ShellView({ tab, onEnded }: { tab: ShellTab; onEnded(): void }) {
  const views = usePtyViews();
  const [failed, setFailed] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  if (!views) return <p className={NOTE}>The shell shows in the app.</p>;
  if (views.status.kind === "pending") return <p className={NOTE}>Getting the terminal ready…</p>;
  if (views.status.kind === "unusable") {
    return (
      <div className={NOTE} data-testid="terminal-unusable">
        <p>The shell can't be shown here: {views.status.message}</p>
        <button type="button" className={RETRY} onClick={() => views.recheck()}>
          Check again
        </button>
      </div>
    );
  }
  if (failed !== null) {
    return (
      <div className={NOTE} data-testid="terminal-view-error">
        <p>Couldn't show the shell: {failed}</p>
        <button
          type="button"
          className={RETRY}
          onClick={() => {
            setFailed(null);
            setAttempt((n) => n + 1);
            views.recheck();
          }}
        >
          Try again
        </button>
      </div>
    );
  }
  return (
    <PtyTerminal
      key={attempt}
      at={tab.at}
      open={views.open}
      copy={views.copy}
      paste={views.paste}
      onView={(v) => {
        if (v.kind === "closed") onEnded();
        if (v.kind === "error") setFailed(plain(v.message));
      }}
    />
  );
}
