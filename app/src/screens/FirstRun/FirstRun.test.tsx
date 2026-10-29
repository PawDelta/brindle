import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Detection, InstallPane, Setup, SignIn, ToolName } from "../../api/setup";
import { FirstRun, Launch } from ".";
import { OUT_OF_DATE } from "./FirstRun";

afterEach(cleanup);

const REQUIRED: ToolName[] = ["uv", "tmux", "brindle", "claude"];

function detection(found: ToolName[], more: Partial<Detection> = {}): Detection {
  return {
    os: "mac",
    tools: [...REQUIRED, "codex", "agy"].map((name) => ({
      name: name as ToolName,
      required: REQUIRED.includes(name as ToolName),
      path: found.includes(name as ToolName) ? `/opt/homebrew/bin/${name}` : null,
      version: found.includes(name as ToolName) ? `${name} 1.0` : null,
    })),
    homebrew: "/opt/homebrew/bin/brew",
    claude_signin: found.includes("claude") ? ("signed_in" as SignIn) : null,
    ...more,
  };
}

/* The shell's setup, scripted: each detect answers the next detection (the
   last one again once they run out), each connect the next outcome (a
   string as the shell's invoke rejects with one). */
function fakeSetup(detections: Detection[], connects: Array<Error | string | null>) {
  const panes: InstallPane[] = [];
  const setup: Setup = {
    detect: vi.fn(async () => (detections.length > 1 ? detections.shift()! : detections[0])),
    connect: vi.fn(async () => {
      const outcome = connects.length > 1 ? connects.shift() : connects[0];
      if (outcome) throw outcome;
    }),
    install: vi.fn(async () => {}),
    attach: vi.fn((pane: InstallPane) => {
      panes.push(pane);
      return { close() {} };
    }),
  };
  /* The install prints a line, and ends, as the last pane hears it. */
  const print = (text: string) => panes.at(-1)!.onData(new TextEncoder().encode(text));
  const exit = (code: number) => panes.at(-1)!.onExit(code);
  return { setup, print, exit, panes };
}

const NOT_INSTALLED = "brindle isn't installed: no `brindle` on PATH, in ~/.local/bin or in Homebrew's bin";

/* Any instruction to go and type a command. */
const COMMANDS = /npm install|brew install|apt-get|dnf install|uv tool (upgrade|update-shell)|source \S+env|Install it with|run:/i;

/* setup.rs's INSTALL_SCRIPT on a clean Mac with Homebrew, as its pty prints it (uv's installer runs quiet). */
const INSTALL_OUTPUT = [
  "\x1b[32m==>\x1b[0m installing uv (Python tool installer, https://docs.astral.sh/uv)\r\n",
  "\x1b[32m==>\x1b[0m installing tmux\r\n",
  "==> Fetching tmux\r\n==> Pouring tmux--3.5a.arm64_sequoia.bottle.tar.gz\r\n🍺  /opt/homebrew/Cellar/tmux/3.5a: 11 files, 1.2MB\r\n",
  "\x1b[32m==>\x1b[0m installing brindle\r\n",
  "Resolved 24 packages in 412ms\r\nInstalled 24 packages in 88ms\r\nInstalled 1 executable: brindle\r\n",
  "\x1b[32m==>\x1b[0m brindle 0.9.2 is installed.\r\n",
];

describe("FirstRun", () => {
  it("onboarding's engine step installs brindle's engine with its lines in the progress card, never a terminal, then starts it and goes on", async () => {
    const { setup, print, exit } = fakeSetup([detection(["claude"])], [null]);
    const onReady = vi.fn();
    render(<FirstRun setup={setup} reason={NOT_INSTALLED} onReady={onReady} />);

    // The first launch's own frame, saying what the install adds.
    const frame = screen.getByTestId("onboarding");
    expect(frame.getAttribute("data-step")).toBe("engine");
    expect(screen.getByTestId("first-run-reason").textContent).toBe(NOT_INSTALLED);
    await waitFor(() => expect(frame.textContent).toContain("Installing it adds uv, brindle, tmux (with Homebrew)"));
    // No terminal, no commands to type, nothing about providers (the first launch's later steps).
    expect(frame.textContent).not.toMatch(/npm install|brew install|Profiles|terminal/i);

    fireEvent.click(screen.getByRole("button", { name: "Install brindle's engine" }));
    await waitFor(() => expect(setup.attach).toHaveBeenCalled());
    print("\x1b[32mInstalling uv\x1b[0m\r\nInstalled brindle 0.9.2\r\n");
    const card = await screen.findByTestId("engine-install");
    await waitFor(() => expect(within(card).getByTestId("engine-install-lines").textContent).toContain("Installed brindle 0.9.2"));
    expect(within(card).getByTestId("engine-install-lines").textContent).toContain("Installing uv");
    expect(card.getAttribute("data-state")).toBe("running");
    expect(setup.connect).not.toHaveBeenCalled();

    // Done: the engine starts, and on into the app.
    exit(0);
    await waitFor(() => expect(onReady).toHaveBeenCalledTimes(1));
    expect(setup.connect).toHaveBeenCalledTimes(1);
  });

  it("onboarding's engine step says when the install fails, keeps its lines, and Retry runs it again inline", async () => {
    const { setup, print, exit } = fakeSetup([detection([])], [null]);
    const onReady = vi.fn();
    render(<FirstRun setup={setup} reason={NOT_INSTALLED} onReady={onReady} />);
    fireEvent.click(await screen.findByRole("button", { name: "Install brindle's engine" }));
    await waitFor(() => expect(setup.attach).toHaveBeenCalled());
    print("error: Failed to download brindle: network unreachable\n");
    exit(1);
    const failed = await screen.findByTestId("engine-install-failed");
    expect(failed.textContent).toContain("It stopped (exit 1)");
    expect(screen.getByTestId("engine-install-lines").textContent).toContain("network unreachable");
    expect(setup.connect).not.toHaveBeenCalled();
    // A second exit for the same run changes nothing.
    exit(0);
    expect(setup.connect).not.toHaveBeenCalled();

    fireEvent.click(within(failed).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(setup.install).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(setup.attach).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId("engine-install-failed")).toBeNull();
    expect(onReady).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("onboarding's engine step names only what the install adds, and says an engine that won't start with Retry", async () => {
    const noBrew = detection(["uv", "brindle"], { homebrew: null });
    const { setup } = fakeSetup([noBrew], [new Error("`brindle daemon` exited (exit status: 2): no such command 'daemon'"), null]);
    const onReady = vi.fn();
    render(<FirstRun setup={setup} reason="`brindle daemon` exited" onReady={onReady} />);
    // Without Homebrew the install adds no tmux (the app ships its own), and says nothing to go and do about it.
    await waitFor(() => expect(screen.getByTestId("onboarding").textContent).toContain("Installing it adds brindle,"));
    expect(screen.getByTestId("onboarding").textContent).not.toMatch(COMMANDS);

    fireEvent.click(screen.getByRole("button", { name: "Try starting it again" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("The engine didn't start:");
    expect(alert.textContent).toContain("no such command 'daemon'");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(onReady).toHaveBeenCalledTimes(1));
  });
});

describe("Launch", () => {
  it("onboarding starts through Launch: the app when the engine is found, else the engine step, which goes on into the app once installed", async () => {
    const found = fakeSetup([detection([...REQUIRED])], [null]);
    render(<Launch setup={found.setup}>{() => <p>the app</p>}</Launch>);
    expect(screen.getByRole("status").textContent).toBe("Starting brindle…");
    expect(await screen.findByText("the app")).toBeTruthy();
    expect(found.setup.detect).not.toHaveBeenCalled();
    cleanup();

    const missing = fakeSetup([detection([]), detection([...REQUIRED])], [new Error(NOT_INSTALLED), null]);
    render(<Launch setup={missing.setup}>{() => <p>the app</p>}</Launch>);
    expect((await screen.findByTestId("first-run-reason")).textContent).toBe(NOT_INSTALLED);
    expect(screen.getByTestId("onboarding").getAttribute("data-step")).toBe("engine");
    expect(screen.queryByText("the app")).toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: "Install brindle's engine" }));
    await waitFor(() => expect(missing.setup.attach).toHaveBeenCalled());

    // What setup.rs's INSTALL_SCRIPT prints on a clean Mac (its own steps, uv's and Homebrew's lines), as the pty sends it.
    for (const chunk of INSTALL_OUTPUT) missing.print(chunk);
    const lines = await screen.findByTestId("engine-install-lines");
    await waitFor(() => expect(lines.textContent).toContain("==> brindle 0.9.2 is installed."));
    // The card keeps the last lines: uv's, as it printed them.
    expect(lines.textContent).toContain("Installed 1 executable: brindle");
    expect(screen.getByTestId("onboarding").textContent).not.toMatch(COMMANDS);

    // Done: the app, and no instruction to type a command anywhere afterwards.
    missing.exit(0);
    expect(await screen.findByText("the app")).toBeTruthy();
    expect(document.body.textContent).not.toMatch(COMMANDS);
  });

  it("waits out an engine's slow first start behind \"Starting brindle…\", and reports only a real failure", async () => {
    // The shell answers that the engine it started is still starting, three times over (its first
    // start, past the shell's own wait), then it listens: never an error, never the setup.
    const STARTING = "engine starting: `brindle daemon` (pid 4242) has been starting for 20s";
    const slow = fakeSetup([detection([...REQUIRED])], [STARTING, STARTING, STARTING, null]);
    render(<Launch setup={slow.setup}>{() => <p>the app</p>}</Launch>);
    const shown: string[] = [];
    await waitFor(
      () => {
        shown.push(screen.queryByRole("status")?.textContent ?? (screen.queryByTestId("first-run-reason") ? "setup" : "app"));
        expect(screen.getByText("the app")).toBeTruthy();
      },
      { timeout: 3000, interval: 20 },
    );
    expect(slow.setup.connect).toHaveBeenCalledTimes(4);
    expect(new Set(shown.slice(0, -1))).toEqual(new Set(["Starting brindle…"]));
    expect(slow.setup.detect).not.toHaveBeenCalled();
    cleanup();

    // One that exits while starting is a failure, with what it said.
    const CRASHED = "`brindle daemon` exited (exit status: 1): Traceback: the engine crashed";
    const crashed = fakeSetup([detection([...REQUIRED])], [STARTING, CRASHED]);
    render(<Launch setup={crashed.setup}>{() => <p>the app</p>}</Launch>);
    expect((await screen.findByTestId("first-run-reason", {}, { timeout: 3000 })).textContent).toBe(CRASHED);
    expect(crashed.setup.connect).toHaveBeenCalledTimes(2);
    cleanup();

    // Meanwhile the screen says what the shell's start is doing: here, stopping the old build's daemon.
    let connected = () => {};
    const replacing = fakeSetup([detection([...REQUIRED])], [null]);
    replacing.setup.connect = () => new Promise<void>((resolve) => (connected = resolve));
    replacing.setup.startStep = vi.fn(async () => ({ step: "stopping" as const, pid: 4242, secs: 2 }));
    render(<Launch setup={replacing.setup}>{() => <p>the app</p>}</Launch>);
    expect((await screen.findByRole("status")).textContent).toContain("4242");
    connected();
    expect(await screen.findByText("the app")).toBeTruthy();
  });

  it("onboarding says an engine too old to start is out of date, without the command that would upgrade it", async () => {
    const TOO_OLD = "engine too old: brindle 0.4.1 has no `daemon` command. Upgrade it: uv tool upgrade brindle";
    const old = fakeSetup([detection([...REQUIRED])], [TOO_OLD, null]);
    render(<Launch setup={old.setup}>{() => <p>the app</p>}</Launch>);
    expect((await screen.findByTestId("first-run-reason")).textContent).toBe(OUT_OF_DATE);
    expect(screen.queryByText("Starting brindle…")).toBeNull();
    expect(screen.queryByTestId("engine-banner")).toBeNull();
    expect(document.body.textContent).not.toMatch(COMMANDS);
    // Once it's up to date the engine starts and the app opens.
    fireEvent.click(await screen.findByRole("button", { name: "Try starting it again" }));
    expect(await screen.findByText("the app")).toBeTruthy();
    expect(screen.queryByTestId("engine-banner")).toBeNull();

    // Any other reason is the engine step alone.
    cleanup();
    const missing = fakeSetup([detection([])], [new Error(NOT_INSTALLED)]);
    render(<Launch setup={missing.setup}>{() => <p>the app</p>}</Launch>);
    await screen.findByTestId("first-run-reason");
    expect(screen.queryByTestId("engine-banner")).toBeNull();
  });
});
