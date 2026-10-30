import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DaemonClient } from "../api/client";
import { DaemonContext } from "../api/context";
import { PtyViewsProvider, type PtyStatus } from "../api/ptyView";
import { READ_SIZE } from "../api/paneText";
import { BASE_URL, FakeDaemon, TOKEN, flush } from "../test/fakeDaemon";
import { STAND_IN_SIZE } from "../test/panes";
import { ProviderSignIn } from "./ProviderSignIn";

vi.mock("./PtyTerminal", () => import("../test/panes"));
// The link the card would open in the browser, recorded.
const opened = vi.hoisted(() => [] as string[]);
vi.mock("../api/open", () => ({ openExternal: (url: string) => (opened.push(url), Promise.resolve()) }));

afterEach(cleanup);

/* The link the card holds now: what "Open browser ↗" opens. */
const link = () => {
  fireEvent.click(within(screen.getByTestId("sign-in-flow")).getByRole("button", { name: "Open browser ↗" }));
  return opened.at(-1);
};

const LINK = "https://accounts.google.com/o/oauth2/auth?client_id=agy";

/* A sign-in to Antigravity over the fake daemon, its pty views as the shell's say `status`;
   `holdLogin`: POST /providers/{p}/login answers only once released (`holdClose`: DELETE /terminals/{id});
   `refuseClose(true)`: DELETE /terminals/{id} fails, the terminal left running. */
function setup(holdLogin = false, holdClose = false) {
  const daemon = new FakeDaemon();
  daemon.data.actionScreens["login antigravity"] = `$ agy\nOpening your browser to sign in: ${LINK}\n`;
  let release: () => void = () => {};
  const held = new Promise<void>((ok) => (release = ok));
  let refusing = false;
  const refuseClose = (on: boolean) => void (refusing = on);
  const fetch: typeof globalThis.fetch = (input, init) =>
    refusing && init?.method === "DELETE"
      ? Promise.resolve(new Response(JSON.stringify({ error: { code: "internal", message: "tmux didn't answer" } }), { status: 500 }))
      : (holdLogin && String(input).endsWith("/login")) || (holdClose && init?.method === "DELETE") ? held.then(() => daemon.fetch(input, init)) : daemon.fetch(input, init);
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch });
  const check = vi.fn(async () => true);
  const ui = (status: PtyStatus) => (
    <DaemonContext.Provider value={client}>
      <PtyViewsProvider value={daemon.ptyViews(status)}>
        <ProviderSignIn provider="antigravity" check={check} />
      </PtyViewsProvider>
    </DaemonContext.Provider>
  );
  const view = render(ui({ kind: "pending" }));
  const shell = (status: PtyStatus) => view.rerender(ui(status));
  const flow = () => screen.getByTestId("sign-in-flow");
  return { daemon, check, shell, flow, release, refuseClose };
}

describe("ProviderSignIn, the CLI read through a pty view", () => {
  it.each([false, true])("confirms a browser login with a pending status read (CLI exited: %s)", async (exited) => {
    const { daemon, check, shell, flow } = setup();
    check.mockResolvedValue(false);
    shell({ kind: "ready", tmux: "tmux 3.7c" });
    vi.useFakeTimers();
    try {
      await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Sign in to Gemini (Antigravity)" })); });
      await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
      check.mockResolvedValue(true);
      let release = () => {};
      if (exited) check.mockImplementationOnce(() => new Promise<boolean>((resolve) => { release = () => resolve(false); }));
      await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
      if (exited) {
        await act(async () => {
          daemon.exitAction(daemon.actions[0].id);
          await vi.advanceTimersByTimeAsync(0);
        });
        expect(flow().dataset.phase).toBe("checking");
        expect(check).toHaveBeenCalledTimes(2);
        await act(async () => { release(); });
      }
      expect(flow().dataset.phase).toBe("done");
      if (exited) expect(check).toHaveBeenCalledTimes(3);
      expect(daemon.actions[0].alive).toBe(!exited);
      expect(daemon.views[0].closed).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("reads the CLI once the shell says the pty view can run, though it said so only after the login answered", async () => {
    const { daemon, check, shell, flow } = setup();
    fireEvent.click(screen.getByRole("button", { name: "Sign in to Gemini (Antigravity)" }));
    await waitFor(() => expect(flow().dataset.phase).toBe("waiting"));
    // Still pending: nothing can read the pane yet, and nothing is opened.
    await flush();
    expect(daemon.views).toEqual([]);
    expect(within(flow()).queryByRole("button", { name: "Show details" })).toBeNull();

    shell({ kind: "ready", tmux: "tmux 3.8" });
    await waitFor(() => expect(daemon.views).toHaveLength(1));
    expect(daemon.views[0].at).toEqual({ server: "brindle", target: daemon.actions[0].target });
    await waitFor(() => expect(within(flow()).getByRole("button", { name: "Open browser ↗" })).toBeTruthy());
    fireEvent.click(within(flow()).getByRole("button", { name: "Copy sign-in link" }));
    await waitFor(() => expect(daemon.clipboard).toBe(LINK));
    expect(within(flow()).getByRole("button", { name: "Show details" })).toBeTruthy();

    // The CLI ends: the provider is read again, and the card says it is signed in.
    daemon.exitAction(daemon.actions[0].id, "Signed in.\n");
    await waitFor(() => expect(flow().dataset.phase).toBe("done"));
    expect(check).toHaveBeenCalledTimes(1);
    expect(daemon.views[0].closed).toBe(true);
  });

  it("reads the CLI when the shell says the pty view can run while the login is still answering", async () => {
    const { daemon, shell, flow, release } = setup(true);
    fireEvent.click(screen.getByRole("button", { name: "Sign in to Gemini (Antigravity)" }));
    await waitFor(() => expect(flow().dataset.phase).toBe("starting"));
    shell({ kind: "ready", tmux: "tmux 3.8" });
    await flush();
    expect(daemon.views).toEqual([]);
    release();
    await waitFor(() => expect(daemon.views).toHaveLength(1));
    await waitFor(() => expect(within(flow()).getByRole("button", { name: "Open browser ↗" })).toBeTruthy());
    daemon.exitAction(daemon.actions[0].id);
    await waitFor(() => expect(flow().dataset.phase).toBe("done"));
  });

  it("says why when the pty view can't run here, rather than waiting for ever", async () => {
    const { daemon, shell, flow } = setup();
    fireEvent.click(screen.getByRole("button", { name: "Sign in to Gemini (Antigravity)" }));
    await waitFor(() => expect(flow().dataset.phase).toBe("waiting"));
    shell({ kind: "unusable", message: "The app's tmux is tmux 3.0a, and brindle needs tmux 3.2 or newer." });
    await waitFor(() => expect(flow().dataset.phase).toBe("failed"));
    expect(within(flow()).getByRole("alert").textContent).toContain("The app can't follow Gemini (Antigravity)'s sign-in: The app's tmux is tmux 3.0a");
    expect(daemon.views).toEqual([]);
  });

  it("with the details open beside it, reads at their size, keeps a long login link whole through the redraw, and takes its own size back once they close", async () => {
    const { daemon, shell, flow } = setup();
    shell({ kind: "ready", tmux: "tmux 3.8" });
    fireEvent.click(screen.getByRole("button", { name: "Sign in to Gemini (Antigravity)" }));
    await waitFor(() => expect(within(flow()).getByRole("button", { name: "Open browser ↗" })).toBeTruthy());
    const [reader] = daemon.views;
    expect(reader.size).toEqual(READ_SIZE);

    // The details: a second client on the pane, narrower; tmux now wraps the pane at its width. The reader takes it too.
    fireEvent.click(within(flow()).getByRole("button", { name: "Show details" }));
    await waitFor(() => expect(reader.resizes).toEqual([[STAND_IN_SIZE.cols, STAND_IN_SIZE.rows]]));
    // tmux redraws the CLI's long link at the details' width, a row at a time (cursor moves, nothing marking a wrap).
    const long = `${LINK}&scope=openid+email&state=${"s".repeat(120)}`;
    const at = (row: number, text: string) => `\x1b[${row};1H${text}`;
    daemon.print(daemon.actions[0].id, `\x1b[H\x1b[2J${at(1, "Open:")}${at(2, long.slice(0, 80))}${at(3, long.slice(80, 160))}${at(4, long.slice(160))}`);
    // A redraw cut short (only the link's first row on screen yet) never cuts the link read.
    await waitFor(() => expect(link()).toBe(long));
    daemon.print(daemon.actions[0].id, `\x1b[H\x1b[2J${at(1, "Open:")}${at(2, long.slice(0, 80))}`);
    await flush();
    await new Promise((r) => setTimeout(r, 50));
    expect(link()).toBe(long);

    // The details close: the reader reads at its own size again.
    fireEvent.click(within(flow()).getByRole("button", { name: "Hide details" }));
    await waitFor(() => expect(reader.resizes.at(-1)).toEqual([READ_SIZE.cols, READ_SIZE.rows]));
  });

  it("starts a fresh sign-in after a failed reader leaves the old login running", async () => {
    const { daemon, shell, flow } = setup();
    shell({ kind: "ready", tmux: "tmux 3.8" });
    fireEvent.click(screen.getByRole("button", { name: "Sign in to Gemini (Antigravity)" }));
    await waitFor(() => expect(daemon.views).toHaveLength(1));
    act(() => daemon.views[0].handlers.onError("Connection lost"));
    await waitFor(() => expect(flow().dataset.phase).toBe("failed"));
    expect(within(flow()).getByRole("alert")).toBeTruthy();
    fireEvent.click(within(flow()).getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(daemon.actions).toHaveLength(2));
    expect(daemon.actions[0].alive).toBe(false);
    expect(daemon.views[0].closed).toBe(true);
    await waitFor(() => expect(flow().dataset.phase).toBe("waiting"));
    daemon.exitAction(daemon.actions[1].id);
    await waitFor(() => expect(flow().dataset.phase).toBe("done"));
  });

  it("login failure frees the terminal: a CLI that fails without exiting is said, Try again replaces it, and Cancel ends it", async () => {
    const { daemon, check, shell, flow, release, refuseClose } = setup(false, true);
    check.mockResolvedValue(false);
    shell({ kind: "ready", tmux: "tmux 3.8" });
    fireEvent.click(screen.getByRole("button", { name: "Sign in to Gemini (Antigravity)" }));
    await waitFor(() => expect(daemon.views).toHaveLength(1));
    // The CLI says its sign-in failed and waits at its own prompt: its pane never dies.
    daemon.print(daemon.actions[0].id, "\r\nLogin failed: OAuth error (invalid_grant). Press Enter to sign in again.\r\n");
    await waitFor(() => expect(flow().dataset.phase).toBe("failed"));
    expect(within(flow()).getByRole("alert").textContent).toContain("Login failed: OAuth error (invalid_grant)");
    fireEvent.click(within(flow()).getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(daemon.actions).toHaveLength(2));
    expect(daemon.actions[0].alive).toBe(false);
    await waitFor(() => expect(flow().dataset.phase).toBe("waiting"));
    // Cancelled, the stuck CLI goes with the card: a press made while it is still closing waits for it, then
    // starts a fresh one rather than attaching to the one going.
    fireEvent.click(within(flow()).getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Sign in to Gemini (Antigravity)" }));
    await flush();
    expect(daemon.actions).toHaveLength(2);
    release();
    await waitFor(() => expect(daemon.actions).toHaveLength(3));
    expect(daemon.closedTerminals).toEqual([daemon.actions[1].id]);
    expect(daemon.actions[2].alive).toBe(true);
    await waitFor(() => expect(flow().dataset.phase).toBe("waiting"));
    // A terminal that couldn't be closed is said; dismissed, the next press still replaces it, never reuses it.
    refuseClose(true);
    fireEvent.click(within(flow()).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(flow().dataset.phase).toBe("failed"));
    expect(within(flow()).getByRole("alert")).toBeTruthy();
    refuseClose(false);
    fireEvent.click(within(flow()).getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: "Sign in to Gemini (Antigravity)" }));
    await waitFor(() => expect(daemon.actions).toHaveLength(4));
    expect(daemon.actions[2].alive).toBe(false);
    await waitFor(() => expect(flow().dataset.phase).toBe("waiting"));
    // Replaced, it is forgotten: the press after that is an ordinary one again.
    fireEvent.click(within(flow()).getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Sign in to Gemini (Antigravity)" }));
    await waitFor(() => expect(daemon.actions).toHaveLength(5));
    expect(daemon.closedTerminals).toEqual([daemon.actions[1].id, daemon.actions[3].id]);
  });

  it("closes its reader when cancelled", async () => {
    const { daemon, shell, flow } = setup();
    shell({ kind: "ready", tmux: "tmux 3.8" });
    fireEvent.click(screen.getByRole("button", { name: "Sign in to Gemini (Antigravity)" }));
    await waitFor(() => expect(daemon.views).toHaveLength(1));
    fireEvent.click(within(flow()).getByRole("button", { name: "Cancel" }));
    expect(daemon.views[0].closed).toBe(true);
  });
});
