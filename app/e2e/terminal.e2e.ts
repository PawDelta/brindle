/* The Terminal docked under a session, in WebKit (WKWebView's engine): the
   status line's ">_ Terminal" opens the panel, whose POST /terminals/shell
   the fake daemon answers with a live shell on a private tmux server (as the
   engine answers with the person's login shell in a window of its own), and
   the panel shows it through a real tmux client on a pty, over the e2e
   tests' bridge (src-tauri/examples/pty_ws.rs, the same PtyView Rust as the
   shell's pty_view_* commands). The agent's pane above it comes the same way.

   Never a person's panes: the server's socket is in the test's own temp
   folder (`-S`), started from an empty config and environment. The screenshot
   (the mockup's size, 1440×900, light) lands in the test's output folder. */

import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";
import { tmuxBin } from "./pty/rig";

const DESKTOP = resolve(dirname(fileURLToPath(import.meta.url)), "..");

let dir = "";
let server = "";
let bridge: ChildProcess | null = null;
let query = "";

test.describe.configure({ mode: "serial", timeout: 120_000 });

test.beforeAll(async ({ browserName }, info) => {
  test.skip(browserName !== "webkit", "the app's webview is WebKit's (WKWebView)");
  test.skip(!tmuxBin(), "needs tmux on PATH");
  test.skip(!existsSync("/bin/zsh"), "needs zsh, the shell the fake daemon names");
  info.setTimeout(300_000);
  dir = realpathSync(mkdtempSync(join(tmpdir(), "brindle-terminal-e2e-")));
  server = join(dir, "tmux.sock");
  const env = { PATH: process.env.PATH ?? "/usr/bin:/bin", HOME: dir, LANG: "en_US.UTF-8", TERM: "xterm-256color" };
  const tmux = (...args: string[]) => execFileSync(tmuxBin()!, ["-S", server, ...args], { encoding: "utf8", env }).trim();
  // The agent's pane (a stand-in: the session above the panel), and a zsh, no rc files, for the panel to show.
  tmux("-f", "/dev/null", "new-session", "-d", "-s", "e2e", "-n", "agent", "-x", "120", "-y", "36", "printf 'a7aaa807 at work\\n'; cat");
  tmux("new-window", "-d", "-t", "e2e:", "-n", "shell", "-c", dir, "env PROMPT='$ ' /bin/zsh -f");
  // The bridge, built as the app's Rust is, serving this page's origin and this server alone.
  execFileSync("cargo", ["build", "--quiet", "--example", "pty_ws", "--manifest-path", join(DESKTOP, "src-tauri/Cargo.toml")], { stdio: "inherit" });
  const origin = `http://127.0.0.1:${new URL(info.project.use.baseURL!).port}`;
  bridge = spawn(join(DESKTOP, "src-tauri/target/debug/examples/pty_ws"), [origin, server], { stdio: ["ignore", "pipe", "inherit"], env: { ...process.env, TMUX: "" } });
  const listening = await new Promise<string>((ok, fail) => {
    createInterface({ input: bridge!.stdout! }).on("line", (line) => line.startsWith("pty_ws listening") && ok(line));
    bridge!.on("exit", (code) => fail(new Error(`pty_ws exited ${code}`)));
  });
  const [, port, token] = listening.match(/127\.0\.0\.1:(\d+) token ([0-9a-f]+)/)!;
  query = new URLSearchParams({ bridge: `ws://127.0.0.1:${port}`, bridgeToken: token, server, target: "e2e:agent", shell: "e2e:shell" }).toString();
});

test.afterAll(() => {
  bridge?.kill();
  if (!server) return;
  try {
    execFileSync(tmuxBin()!, ["-S", server, "kill-server"]);
  } catch {
    // gone already
  }
  rmSync(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
});

/* The panel's shell, row by row (the DOM renderer's, which draws some spaces as no-break ones). */
const shellRows = async (page: Page) =>
  (await page.locator('[data-testid="terminal-panel"] [data-testid="terminal"] .xterm-rows > div').allTextContents()).map((r) => r.replace(/ /g, " "));

test("terminal panel: the status line's toggle docks a live shell under the session", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/shots/preview.html?scene=pty&theme=light&renderer=dom&${query}`);
  await page.waitForSelector("html[data-ready]");
  const surface = page.getByTestId("terminal-surface");
  await expect(surface.getByTestId("terminal")).toHaveAttribute("data-view", "live");
  await expect(page.getByTestId("terminal-panel")).toHaveCount(0);

  // Open from the sidebar's status line: one tab, named by the repo, saying where the shell started.
  const toggle = page.getByTestId("status-line").getByTestId("terminal-toggle");
  await expect(toggle).toHaveText(">_Terminal⌃`");
  await toggle.click();
  const panel = page.getByTestId("terminal-panel");
  await expect(panel.getByRole("tab")).toHaveText([">_Terminalpawdelta"]);
  await expect(panel.getByTestId("terminal-where")).toHaveText("zsh, started in a7aaa807's worktree~/Projects/pawdelta/.brindle/worktrees/desktop/ux-revamp");
  const shell = panel.getByTestId("terminal");
  await expect(shell).toHaveAttribute("data-target", "e2e:shell");
  await expect(shell).toHaveAttribute("data-view", "live");

  // A live shell: what is typed runs, and its answer comes back.
  await expect.poll(async () => (await shellRows(page)).some((r) => r.startsWith("$"))).toBe(true);
  await shell.click();
  await page.keyboard.type("echo $((6*7))");
  await page.keyboard.press("Enter");
  await expect.poll(async () => (await shellRows(page)).map((r) => r.trim())).toContain("42");

  // Docked, not over the session: under it, the session still showing, at about a third of the column.
  const session = (await surface.boundingBox())!;
  const docked = (await panel.boundingBox())!;
  expect(docked.y).toBeGreaterThanOrEqual(session.y + session.height);
  expect(session.height).toBeGreaterThan(docked.height);
  expect(docked.height / 900).toBeGreaterThan(0.25);
  expect(docked.height / 900).toBeLessThan(0.45);
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
  await page.screenshot({ path: test.info().outputPath("terminal-open-light.png") });

  // ⌃` hides it; the session has the column again.
  await page.keyboard.press("Control+Backquote");
  await expect(panel).toHaveCount(0);
});

