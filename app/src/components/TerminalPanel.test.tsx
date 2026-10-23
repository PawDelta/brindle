import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DaemonClient } from "../api/client";
import { PtyViewsProvider, type PtyViewHandlers, type PtyViews } from "../api/ptyView";
import type { WorkspaceDTO } from "../api/schema";
import { BASE_URL, FakeDaemon, TOKEN } from "../test/fakeDaemon";
import { standInViews } from "../test/panes";
import type { PtyTerminalProps } from "./PtyTerminal";
import { TerminalPanel, TerminalProvider, TerminalToggle, shellPlace } from "./TerminalPanel";

/* PtyTerminal's stand-in: it names the pane it shows, and says its view is live, or (a target in `unattached`) that it couldn't attach.
   With `real.on`, the real PtyTerminal (xterm.js in jsdom), its xterm.js recorded in `xterms`, to select in it. */
const unattached = vi.hoisted(() => new Set<string>());
const real = vi.hoisted(() => ({ on: false }));
const xterms = vi.hoisted(() => [] as import("@xterm/xterm").Terminal[]);
vi.mock("@xterm/xterm", async (original) => {
  const actual = await original<typeof import("@xterm/xterm")>();
  class Recorded extends actual.Terminal {
    constructor(...args: ConstructorParameters<typeof actual.Terminal>) {
      super(...args);
      xterms.push(this);
    }
  }
  return { ...actual, Terminal: Recorded };
});
vi.mock("./PtyTerminal", async (original) => {
  const actual = await original<typeof import("./PtyTerminal")>();
  return {
    PtyTerminal: (props: PtyTerminalProps) => {
      const { at, onView } = props;
      useEffect(() => {
        if (!real.on) onView?.(unattached.has(at.target) ? { kind: "error", message: `can't find pane: ${at.target}` } : { kind: "live" });
      }, [at.target, onView]);
      return real.on ? <actual.PtyTerminal {...props} /> : <div data-testid="terminal" data-target={at.target} />;
    },
  };
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  unattached.clear();
  real.on = false;
  xterms.length = 0;
  vi.unstubAllGlobals();
});

const REPO = "/home/sam/Projects/pawdelta";
const WORKTREE = `${REPO}/.brindle/worktrees/docs/readme`;
const repos = [{ repo_root: REPO, name: "pawdelta" }];
const workspaces: WorkspaceDTO[] = [
  { id: "pawdelta/main", name: "main", branch: "main", path: REPO, agents: [{ id: "sup-42" } as never] },
  { id: "pawdelta/docs-readme", name: "docs-readme", branch: "docs/readme", path: WORKTREE, agents: [{ id: "a7aaa807" } as never] },
];
const here = shellPlace({ screen: "session", root: "sup-42", repo: REPO, agent: "a7aaa807" }, repos, workspaces);

/* The panel and its toggle over the fake daemon, whose routes in `failing` ("POST /v1/terminals/shell") fail once
   with a 500, and those in `holding` answer only on release(), all together. */
function setup(views: PtyViews = standInViews) {
  const daemon = new FakeDaemon({ repos, workspaces });
  const failing: string[] = [];
  const holding: string[] = [];
  let release: () => void = () => {};
  const held = new Promise<void>((ok) => (release = ok));
  const fetch: typeof globalThis.fetch = async (input, init) => {
    const key = `${init?.method ?? "GET"} ${new URL(String(input)).pathname}`;
    if (holding.includes(key)) await held;
    const at = failing.indexOf(key);
    if (at < 0) return daemon.fetch(input, init);
    failing.splice(at, 1);
    return new Response(JSON.stringify({ error: { code: "internal", message: "tmux said no" } }), { status: 500 });
  };
  const ui = (v: PtyViews) => (
    <PtyViewsProvider value={v}>
      <TerminalProvider client={new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch })} here={here}>
        <TerminalToggle />
        <TerminalPanel />
      </TerminalProvider>
    </PtyViewsProvider>
  );
  const view = render(ui(views));
  return { daemon, failing, holding, release, rerender: (v: PtyViews) => view.rerender(ui(v)) };
}

describe("TerminalPanel", () => {
  it("starts a shell where the open session's agent works, else at the repo root", () => {
    expect(here).toEqual({ repo: REPO, name: "pawdelta", cwd: WORKTREE, why: "started in a7aaa807's worktree" });
    expect(shellPlace({ screen: "session", root: "sup-42", repo: REPO }, repos, workspaces)).toMatchObject({ cwd: REPO, why: "started at the repo root" });
    expect(shellPlace({ screen: "settings" }, repos, workspaces)).toMatchObject({ cwd: REPO, why: "started at the repo root" });
  });

  it("opens a shell from the status line's toggle, another on +, and closes the shown one on ×, saying when either fails", async () => {
    const { daemon, failing } = setup();
    const calls = () => daemon.requests.filter((r) => r.path.startsWith("/terminals")).map((r) => `${r.method} ${r.path}`);
    expect(screen.queryByTestId("terminal-panel")).toBeNull();

    // Open: a shell in a7aaa807's worktree, its tab named by the repo, the bar saying where it started.
    fireEvent.click(screen.getByTestId("terminal-toggle"));
    const panel = await screen.findByTestId("terminal-panel");
    await waitFor(() => expect(within(panel).getAllByRole("tab")).toHaveLength(1));
    expect(daemon.shells).toEqual([{ terminal: daemon.actions[0].id, cwd: WORKTREE }]);
    expect(within(panel).getByRole("tab").textContent).toBe(">_Terminalpawdelta");
    expect(screen.getByTestId("terminal-where").textContent).toBe("zsh, started in a7aaa807's worktree~/Projects/pawdelta/.brindle/worktrees/docs/readme");
    expect(within(panel).getByTestId("terminal").dataset.target).toBe(daemon.actions[0].target);
    expect(screen.getByTestId("terminal-toggle").getAttribute("aria-pressed")).toBe("true");

    // + refused: it says so, and the shell shown stays.
    failing.push("POST /v1/terminals/shell");
    fireEvent.click(within(panel).getByRole("button", { name: "Another shell" }));
    expect((await within(panel).findByRole("alert")).textContent).toBe("Couldn't start a shell: tmux said no.");
    expect(within(panel).getAllByRole("tab")).toHaveLength(1);
    expect(within(panel).getByTestId("terminal").dataset.target).toBe(daemon.actions[0].target);

    // + again: another shell in the same place, shown.
    fireEvent.click(within(panel).getByRole("button", { name: "Another shell" }));
    await waitFor(() => expect(within(panel).getAllByRole("tab")).toHaveLength(2));
    expect(within(panel).queryByRole("alert")).toBeNull();
    expect(within(panel).getAllByRole("tab")[1].getAttribute("aria-selected")).toBe("true");
    expect(within(panel).getByTestId("terminal").dataset.target).toBe(daemon.actions[1].target);

    // × refused: it says so, and the tab stays, for × to try again.
    failing.push(`DELETE /v1/terminals/${daemon.actions[1].id}`);
    fireEvent.click(within(panel).getByRole("button", { name: "Close this shell" }));
    expect((await within(panel).findByRole("alert")).textContent).toBe("Couldn't close the shell: tmux said no.");
    expect(within(panel).getAllByRole("tab")).toHaveLength(2);
    expect(daemon.closedTerminals).toEqual([]);

    // × again: the shown shell is closed on the daemon; the other stays, shown.
    fireEvent.click(within(panel).getByRole("button", { name: "Close this shell" }));
    await waitFor(() => expect(daemon.closedTerminals).toEqual([daemon.actions[1].id]));
    await waitFor(() => expect(within(panel).getAllByRole("tab")).toHaveLength(1));
    expect(within(panel).getByTestId("terminal").dataset.target).toBe(daemon.actions[0].target);
    expect(calls()).toEqual(["POST /terminals/shell", "POST /terminals/shell", `DELETE /terminals/${daemon.actions[1].id}`]);

    // Collapsed (⌃`), the shell keeps running; open again, it is shown again without a new one.
    fireEvent.keyDown(window, { key: "`", code: "Backquote", ctrlKey: true });
    await waitFor(() => expect(screen.queryByTestId("terminal-panel")).toBeNull());
    fireEvent.click(screen.getByTestId("terminal-toggle"));
    expect(within(await screen.findByTestId("terminal-panel")).getByTestId("terminal").dataset.target).toBe(daemon.actions[0].target);
    expect(calls()).toHaveLength(3);
  });

  it("keeps a shell started on + while the shell before it closes, the two answers landing together", async () => {
    const { daemon, holding, release } = setup();
    fireEvent.click(screen.getByTestId("terminal-toggle"));
    const panel = await screen.findByTestId("terminal-panel");
    await waitFor(() => expect(within(panel).getAllByRole("tab")).toHaveLength(1));
    const first = daemon.actions[0].id;

    // × on the first and + for a second, both answered only once both are asked; then together.
    holding.push(`DELETE /v1/terminals/${first}`, "POST /v1/terminals/shell");
    fireEvent.click(within(panel).getByRole("button", { name: "Close this shell" }));
    fireEvent.click(within(panel).getByRole("button", { name: "Another shell" }));
    release();
    await waitFor(() => expect(daemon.closedTerminals).toEqual([first]));
    await waitFor(() => expect(daemon.actions).toHaveLength(2));
    const second = daemon.actions[1];
    // The second shell's tab alone, shown, the panel still open.
    await waitFor(() => expect(within(screen.getByTestId("terminal-panel")).getByTestId("terminal").dataset.target).toBe(second.target));
    expect(within(screen.getByTestId("terminal-panel")).getAllByRole("tab")).toHaveLength(1);
    expect(within(screen.getByTestId("terminal-panel")).getByRole("tab").getAttribute("aria-selected")).toBe("true");
    expect(screen.getByTestId("terminal-toggle").getAttribute("aria-pressed")).toBe("true");
  });

  it("says why a shell can't be shown, keeps its tab, and tries again", async () => {
    const recheck = vi.fn();
    const unusable: PtyViews = { ...standInViews, status: { kind: "unusable", message: "tmux 3.2 or later is needed." }, recheck };
    const { daemon, rerender } = setup(unusable);
    fireEvent.click(screen.getByTestId("terminal-toggle"));
    const panel = await screen.findByTestId("terminal-panel");

    // No pty view here: why, and Check again asks the shell again.
    expect((await within(panel).findByTestId("terminal-unusable")).textContent).toContain("The shell can't be shown here: tmux 3.2 or later is needed.");
    expect(within(panel).queryByTestId("terminal")).toBeNull();
    fireEvent.click(within(panel).getByRole("button", { name: "Check again" }));
    expect(recheck).toHaveBeenCalledTimes(1);

    // Ready, but the view can't attach: why, the tab kept; Try again attaches afresh.
    const target = daemon.actions[0].target;
    unattached.add(target);
    rerender({ ...standInViews, recheck });
    expect((await within(panel).findByTestId("terminal-view-error")).textContent).toContain(`Couldn't show the shell: can't find pane: ${target}.`);
    expect(within(panel).getAllByRole("tab")).toHaveLength(1);
    unattached.clear();
    fireEvent.click(within(panel).getByRole("button", { name: "Try again" }));
    expect((await within(panel).findByTestId("terminal")).dataset.target).toBe(target);
    expect(recheck).toHaveBeenCalledTimes(2);
    expect(daemon.closedTerminals).toEqual([]);
  });

  it("select to copy and ⌘V paste in the shell go through the app's clipboard, not the webview's", async () => {
    real.on = true;
    vi.stubGlobal("matchMedia", () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));
    const written: string[] = [];
    const copied: string[] = [];
    const clipboard = { text: "" };
    let handlers: PtyViewHandlers | undefined;
    const views: PtyViews = {
      ...standInViews,
      open: (_at, _size, h) => {
        handlers = h;
        return { write: (d) => void written.push(d), resize() {}, focus() {}, close() {} };
      },
      copy: (text) => (copied.push(text), Promise.resolve()),
      paste: () => Promise.resolve(clipboard.text),
    };
    setup(views);
    fireEvent.click(screen.getByTestId("terminal-toggle"));
    const shell = await within(await screen.findByTestId("terminal-panel")).findByTestId("terminal");
    await waitFor(() => expect(handlers).toBeDefined());
    const term = xterms.at(-1)!;
    await act(() => {
      handlers!.onData(new TextEncoder().encode("\x1b[?2004h$ echo copy-me\r\ncopy-me\r\n$ "));
      return new Promise<void>((r) => term.write("", r));
    });

    // A drag over "copy-me": on the app's clipboard as the mouse lets go.
    act(() => void shell.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));
    act(() => term.select(0, 1, 7));
    act(() => void window.dispatchEvent(new MouseEvent("mouseup", { bubbles: true })));
    expect(copied).toEqual(["copy-me"]);

    // ⌘V (off a Mac here): the app's clipboard, pasted into the shell as a bracketed paste.
    clipboard.text = "pasted from pbcopy";
    const key = new KeyboardEvent("keydown", { key: "v", metaKey: true, bubbles: true, cancelable: true });
    act(() => void term.textarea!.dispatchEvent(key));
    expect(key.defaultPrevented).toBe(true);
    await act(() => new Promise((r) => setTimeout(r, 0)));
    expect(written).toEqual(["\x1b[200~pasted from pbcopy\x1b[201~"]);
  });

  it("a copy in the shell says \"Copied\" in the panel: tmux's drag (OSC 52, the mouse reported) and a selection let go", async () => {
    real.on = true;
    vi.stubGlobal("matchMedia", () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));
    const copied: string[] = [];
    let handlers: PtyViewHandlers | undefined;
    const views: PtyViews = {
      ...standInViews,
      open: (_at, _size, h) => {
        handlers = h;
        return { write() {}, resize() {}, focus() {}, close() {} };
      },
      copy: (text) => (copied.push(text), Promise.resolve()),
    };
    setup(views);
    fireEvent.click(screen.getByTestId("terminal-toggle"));
    const panel = await screen.findByTestId("terminal-panel");
    const shell = await within(panel).findByTestId("terminal");
    await waitFor(() => expect(handlers).toBeDefined());
    const term = xterms.at(-1)!;
    const fed = (bytes: string) =>
      act(() => {
        handlers!.onData(new TextEncoder().encode(bytes));
        return new Promise<void>((r) => term.write("", r));
      });
    // tmux has the mouse reported; a plain drag is tmux's copy mode, which copies on release as OSC 52.
    await fed("\x1b[?1002h\x1b[?1006h$ echo copy-me\r\ncopy-me\r\n$ ");
    expect(within(panel).queryByTestId("terminal-copied")).toBeNull();
    await fed(`\x1b]52;;${btoa("copy-me")}\x07`);
    await waitFor(() => expect(copied).toEqual(["copy-me"]));
    expect(within(panel).getByTestId("terminal-copied").textContent).toBe("Copied");
    await waitFor(() => expect(within(panel).queryByTestId("terminal-copied")).toBeNull(), { timeout: 3000 });
    // An Option-drag's selection let go: the same.
    act(() => void shell.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));
    act(() => term.select(0, 1, 7));
    act(() => void window.dispatchEvent(new MouseEvent("mouseup", { bubbles: true })));
    expect(copied).toEqual(["copy-me", "copy-me"]);
    expect((await within(panel).findByTestId("terminal-copied")).textContent).toBe("Copied");
  });
});
