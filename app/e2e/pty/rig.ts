/* What e2e/pty.e2e.ts runs against: a live Claude Code on a tmux server of
   its own, and the pty view's bridge to it. Never a person's panes:

   - The tmux server is private (`-f /dev/null`), started from an empty
     environment, its socket in the rig's own temp folder (`-S`), not
     tmux's shared folder for this user: a tool sweeping that folder's
     servers (an engine looking for its agents) never finds it.
   - Claude Code runs with a HOME and a CLAUDE_CONFIG_DIR of its own, in a
     temp folder: onboarded and trusting its project by config, its
     fullscreen renderer on (`tui: fullscreen`: the alternate screen, the
     mouse its own, "Jump to bottom"), and a placeholder API key it is told
     to accept. It resumes a conversation written here (150 numbered history
     lines) and talks to no API: its base URL is a closed port, and nothing
     typed is ever submitted. One the typing test asks for (`working`), in a
     window of its own on a conversation of its own, is submitted to: it
     talks to a stand-in API on localhost (fake_api.mjs), which streams it a
     long answer slowly, so it is at work while it is typed into.
   - The bridge is src-tauri/examples/pty_ws.rs, the same PtyView Rust as
     the shell's pty_view_* commands, serving only this page's origin, with
     a token, on that server alone. It runs in the app's environment: no
     locale, as launchd starts the app. */

import { execFileSync, spawn, spawnSync, type ChildProcess } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

const DESKTOP = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const MANIFEST = join(DESKTOP, "src-tauri/Cargo.toml");
export const LINES = 150;

/* The tool on PATH, or null. */
function which(tool: string): string | null {
  const found = spawnSync("/usr/bin/which", [tool], { encoding: "utf8" });
  return found.status === 0 ? found.stdout.trim() : null;
}

export const tmuxBin = () => which("tmux");
export const claudeBin = () => which("claude");

export interface Rig {
  /* The tmux server's socket path. */
  server: string;
  /* The agent's window: `agents:a1`. */
  target: string;
  /* The page's query for the shots scene "pty", on Claude Code's window or (`agents:plain`) a plain pane's. */
  query: string;
  queryFor(target: string): string;
  tmux(...args: string[]): string;
  /* What the pane shows, as text. */
  screen(): string;
  /* A Claude Code in window `window` that answers what is submitted to it from a stand-in API, slowly (it works
     a while, its answer streaming in): the page's query for it. */
  working(window: string): Promise<string>;
  stop(): void;
}

/* The conversation Claude Code resumes: a question, and an answer of numbered lines. */
function transcript(id: string, cwd: string): string {
  const base = { isSidechain: false, userType: "external", entrypoint: "cli", cwd, sessionId: id, version: "2.1.306" };
  const asked = randomUUID();
  const at = Date.now();
  const lines = [
    { ...base, parentUuid: null, type: "user", uuid: asked, timestamp: new Date(at - 60_000).toISOString(), message: { role: "user", content: `Print ${LINES} numbered history lines.` } },
    {
      ...base,
      parentUuid: asked,
      type: "assistant",
      uuid: randomUUID(),
      requestId: "req_e2e",
      timestamp: new Date(at - 50_000).toISOString(),
      message: {
        id: `msg_${randomUUID().replace(/-/g, "")}`,
        type: "message",
        role: "assistant",
        model: "claude-haiku-4-5",
        content: [{ type: "text", text: Array.from({ length: LINES }, (_, i) => `history line ${i + 1}`).join("\n") }],
        stop_reason: "end_turn",
        stop_sequence: null,
        usage: { input_tokens: 10, output_tokens: 600 },
      },
    },
  ];
  return lines.map((l) => JSON.stringify(l)).join("\n") + "\n";
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/* Claude Code up on the private server, in a window of `cols` x `rows`, and the bridge for `origin`. */
export async function startRig(origin: string, cols = 120, rows = 36): Promise<Rig> {
  const tmux = tmuxBin()!;
  const claude = claudeBin()!;
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "brindle-pty-e2e-")));
  const home = join(dir, "home");
  const config = join(dir, "config");
  const project = join(dir, "proj");
  for (const d of [home, config, project]) mkdirSync(d, { recursive: true });
  const key = `sk-ant-api03-${"e2e".repeat(27)}placeholder-not-a-key`;
  writeFileSync(
    join(config, ".claude.json"),
    JSON.stringify({
      hasCompletedOnboarding: true,
      theme: "dark",
      numStartups: 5,
      customApiKeyResponses: { approved: [key.slice(-20)], rejected: [] },
      projects: { [project]: { hasTrustDialogAccepted: true, hasCompletedProjectOnboarding: true, allowedTools: [] } },
    }),
  );
  writeFileSync(join(config, "settings.json"), JSON.stringify({ tui: "fullscreen" }));
  const id = randomUUID();
  const slug = project.replace(/[^a-zA-Z0-9]/g, "-");
  mkdirSync(join(config, "projects", slug), { recursive: true });
  writeFileSync(join(config, "projects", slug, `${id}.jsonl`), transcript(id, project));
  const launcher = join(dir, "claude.sh");
  writeFileSync(launcher, `cd ${JSON.stringify(project)} && exec ${JSON.stringify(claude)} --resume ${id}\n`);

  // The server's socket: a path, which the bridge and the view name it by too (`-S`).
  const server = join(dir, "tmux.sock");
  const env = {
    PATH: process.env.PATH ?? "/usr/bin:/bin",
    HOME: home,
    LANG: "en_US.UTF-8",
    TERM: "xterm-256color",
    CLAUDE_CONFIG_DIR: config,
    ANTHROPIC_API_KEY: key,
    ANTHROPIC_BASE_URL: "http://127.0.0.1:9",
    DISABLE_AUTOUPDATER: "1",
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
  };
  const run = (...args: string[]) => execFileSync(tmux, ["-S", server, ...args], { encoding: "utf8", env }).trim();
  run("-f", "/dev/null", "new-session", "-d", "-s", "agents", "-n", "a1", "-x", String(cols), "-y", String(rows), `sh ${JSON.stringify(launcher)}`);
  // A plain pane beside it, no program's mouse: tmux's own wheel (copy mode) and copy.
  run("new-window", "-d", "-t", "agents:", "-n", "plain", "seq 1 200; printf 'copymeplease and more\\n'; cat");
  // A Claude Code that exits leaves its last words on screen (this server's own window).
  run("set-option", "-w", "-t", "agents:a1", "remain-on-exit", "on");
  // Claude Code puts a no-break space after its prompt's ❯: read as a space.
  const screen = () => run("capture-pane", "-p", "-t", "agents:a1").replace(/ /g, " ");
  const deadline = Date.now() + 60_000;
  // Up: the conversation's foot on screen, the prompt below it, and the mouse Claude Code's own.
  while (!(screen().includes(`history line ${LINES}`) && screen().includes("❯") && run("display-message", "-p", "-t", "agents:a1", "#{alternate_on}#{mouse_any_flag}#{mouse_sgr_flag}") === "111")) {
    if (Date.now() > deadline) {
      const shown = screen();
      run("kill-server");
      throw new Error(`Claude Code didn't come up on the private tmux server:\n${shown}`);
    }
    await sleep(250);
  }
  // Drawn is not settled: a Claude Code just up lets its first wheel reports go by for a moment.
  await sleep(3000);

  // The bridge, built as the app's Rust is.
  execFileSync("cargo", ["build", "--quiet", "--example", "pty_ws", "--manifest-path", MANIFEST], { stdio: "inherit" });
  // In the app's environment: launchd starts it with no locale (no LANG, no LC_*), and so its tmux commands and client.
  const appEnv = Object.fromEntries(Object.entries(process.env).filter(([k]) => k !== "LANG" && !k.startsWith("LC_")));
  const bridge: ChildProcess = spawn(join(DESKTOP, "src-tauri/target/debug/examples/pty_ws"), [origin, server], { stdio: ["ignore", "pipe", "inherit"], env: { ...appEnv, TMUX: "" } });
  const listening = await new Promise<string>((ok, fail) => {
    const lines = createInterface({ input: bridge.stdout! });
    lines.on("line", (line) => line.startsWith("pty_ws listening") && ok(line));
    bridge.on("exit", (code) => fail(new Error(`pty_ws exited ${code}`)));
  });
  const [, port, token] = listening.match(/127\.0\.0\.1:(\d+) token ([0-9a-f]+)/)!;
  try {
    if (run("display-message", "-p", "-t", "agents:a1", "#{pane_dead}") !== "0") throw new Error("Claude Code exited");
  } catch (e) {
    bridge.kill();
    throw new Error(`the private tmux server's Claude Code went: ${e}`);
  }
  const queryFor = (target: string) => new URLSearchParams({ bridge: `ws://127.0.0.1:${port}`, bridgeToken: token, server, target }).toString();

  let api: ChildProcess | null = null;
  let apiPort = "";
  return {
    server,
    target: "agents:a1",
    query: queryFor("agents:a1"),
    queryFor,
    tmux: run,
    screen,
    async working(window: string) {
      // The stand-in API (fake_api.mjs), once: a long answer, a word every 40 ms.
      if (!api) {
        const started = spawn(process.execPath, [join(DESKTOP, "e2e/pty/fake_api.mjs"), "3000", "40"], { stdio: ["pipe", "pipe", "inherit"] });
        api = started;
        apiPort = await new Promise<string>((ok, fail) => {
          createInterface({ input: started.stdout! }).on("line", (line) => line.startsWith("fake_api listening on ") && ok(line.split(" ").at(-1)!));
          started.on("exit", (code) => fail(new Error(`fake_api exited ${code}`)));
        });
      }
      // A Claude Code of its own, on a conversation of its own, talking to it.
      const work = join(dir, `${window}.sh`);
      writeFileSync(work, `cd ${JSON.stringify(project)} && ANTHROPIC_BASE_URL=http://127.0.0.1:${apiPort} exec ${JSON.stringify(claude)}\n`);
      run("new-window", "-d", "-t", "agents:", "-n", window, `sh ${JSON.stringify(work)}`);
      const shown = () => run("capture-pane", "-p", "-t", `agents:${window}`).replace(/ /g, " ");
      const deadline = Date.now() + 60_000;
      while (!(shown().includes("❯") && run("display-message", "-p", "-t", `agents:${window}`, "#{alternate_on}#{mouse_any_flag}") === "11")) {
        if (Date.now() > deadline) throw new Error(`Claude Code didn't come up in agents:${window}:\n${shown()}`);
        await sleep(250);
      }
      await sleep(2000);
      return queryFor(`agents:${window}`);
    },
    stop() {
      api?.kill();
      bridge.kill();
      try {
        run("kill-server");
      } catch {
        // gone already
      }
      // Its socket with it: tmux leaves the file behind. Retried: Claude Code may still be writing its config as it exits.
      rmSync(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
    },
  };
}

/* A person's terminal on the agent's own session (native_client.py): `type` into it, `close` to detach. */
export function nativeClient(rig: Rig, cols: number, rows: number): Promise<{ type(text: string): void; close(): Promise<void> }> {
  const child = spawn("python3", [join(DESKTOP, "e2e/pty/native_client.py"), tmuxBin()!, rig.server, "agents", String(cols), String(rows)], { stdio: ["pipe", "pipe", "inherit"] });
  return new Promise((ok, fail) => {
    const lines = createInterface({ input: child.stdout! });
    lines.on("line", (line) => {
      if (line !== "attached") return;
      ok({
        type: (text) => child.stdin!.write(text),
        close: () =>
          new Promise<void>((done) => {
            child.on("exit", () => done());
            child.stdin!.end();
          }),
      });
    });
    child.on("exit", (code) => fail(new Error(`native_client exited ${code}`)));
  });
}
