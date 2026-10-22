/* What the preview's fake daemon serves: a realistic afternoon. Two repos;
   in the first a live autopilot session with a supervisor and four workers
   (one at work, one waiting on a person, one reported and awaiting review,
   one failed), a goal with milestones, branch diffs and terminal panes; in
   the second a paused session. The inbox has one of each thing that can
   wait on a person. */

import { fakeBlob, type DaemonData, type Pane, type RoutingProfile } from "../src/test/fakeDaemon";
import type { AgentDTO, DecisionDTO, GoalDTO, InboxItemDTO, ProfileDTO, SessionDTO, WorkspaceDTO } from "../src/api/schema";

const now = Date.now() / 1000;
export const REPO = "/home/sam/Projects/pawdelta";
export const OTHER = "/home/sam/Projects/brindle";
/* brindle's home (the folder of meta.db_path): a scratch session's repo lives under its scratch/. */
export const BRINDLE_HOME = "/home/sam/.brindle";
const SCRATCH = `${BRINDLE_HOME}/scratch/notes-20261020-091200`;

/* An agent, in the tmux window the engine names for it (where the app's tmux client attaches). */
const agent = (a: Partial<AgentDTO> & Pick<AgentDTO, "id" | "status" | "mode">): AgentDTO => ({
  profile: "claude",
  provider: "claude",
  subagents: [],
  window: `brindle:${a.id}`,
  tmux_server: "brindle",
  ...a,
});

const workspaces: WorkspaceDTO[] = [
  // A scratch session (brindle run outside a repo): its work can be moved into one.
  {
    id: "notes-20261020-091200",
    name: "notes-20261020-091200",
    branch: "main",
    base_branch: null,
    path: SCRATCH,
    ahead: 0,
    behind: 0,
    dirty: 1,
    agents: [agent({ id: "scr-3", status: "idle", mode: "supervisor", status_since: now - 700 })],
    review: null,
    review_state: null,
    autopilot: false,
    asking: null,
  },
  {
    id: "pawdelta",
    name: "pawdelta",
    branch: "dev/desktop-app",
    base_branch: null,
    path: REPO,
    ahead: 0,
    behind: 0,
    dirty: 0,
    agents: [agent({ id: "sup-42", status: "idle", mode: "supervisor", status_since: now - 240, tokens: "118k" })],
    review: null,
    review_state: null,
    autopilot: true,
    asking: null,
  },
  {
    id: "pawdelta/desktop-ux-revamp",
    name: "desktop-ux-revamp",
    branch: "desktop/ux-revamp",
    base_branch: "dev/desktop-app",
    path: `${REPO}/.brindle/worktrees/desktop/ux-revamp`,
    ahead: 3,
    behind: 0,
    dirty: 2,
    agents: [
      agent({ id: "a7aaa807", status: "processing", mode: "worker", parent_id: "sup-42", status_since: now - 95, tokens: "64k",
        subagents: [{ id: "sa1", agent_type: "Explore", started_at: now - 40 }] }),
    ],
    review: null,
    review_state: null,
    autopilot: false,
    asking: null,
  },
  {
    id: "pawdelta/desktop-shots",
    name: "desktop-shots",
    branch: "desktop/shots",
    base_branch: "dev/desktop-app",
    path: `${REPO}/.brindle/worktrees/desktop/shots`,
    ahead: 1,
    behind: 2,
    dirty: 0,
    agents: [agent({ id: "b31c9f02", status: "waiting", mode: "worker", parent_id: "sup-42", status_since: now - 610, tokens: "22k" })],
    review: null,
    review_state: null,
    autopilot: false,
    asking: null,
  },
  {
    id: "pawdelta/docs-readme",
    name: "docs-readme",
    branch: "docs/readme",
    base_branch: "dev/desktop-app",
    path: `${REPO}/.brindle/worktrees/docs/readme`,
    ahead: 2,
    behind: 0,
    dirty: 0,
    agents: [
      agent({ id: "c58d1e77", status: "idle", mode: "worker", parent_id: "sup-42", status_since: now - 1900, reported: true, tokens: "31k" }),
      agent({ id: "d02f44a1", status: "idle", mode: "worker", parent_id: "c58d1e77", status_since: now - 300, provider: "codex", profile: "reviewer-codex" }),
    ],
    review: null,
    review_state: "pending",
    autopilot: false,
    asking: null,
  },
  {
    id: "pawdelta/fix-tray-badge",
    name: "fix-tray-badge",
    branch: "fix/tray-badge",
    base_branch: "dev/desktop-app",
    path: `${REPO}/.brindle/worktrees/fix/tray-badge`,
    ahead: 1,
    behind: 0,
    dirty: 0,
    agents: [agent({ id: "e9a0b6c3", status: "failed", mode: "worker", parent_id: "sup-42", status_since: now - 3300 })],
    review: null,
    review_state: null,
    autopilot: false,
    asking: null,
  },
];

const sessions: SessionDTO[] = [
  { id: "sup-42", workspace_id: "pawdelta", profile: "supervisor", status: "idle", mode: "interactive", created_at: now - 5400, goal: "UX revamp of the brindle desktop app" },
  { id: "sup-39", workspace_id: "pawdelta", profile: "supervisor", status: "paused", mode: "interactive", created_at: now - 3 * 86400, status_since: now - 2 * 86400, goal: "Terminals through the shell" },
  { id: "sup-7", workspace_id: "brindle", profile: "supervisor", status: "paused", mode: "interactive", created_at: now - 9 * 86400, status_since: now - 86400 * 4, goal: "Daemon: terminal route and liveness sweep" },
  { id: "scr-3", workspace_id: "notes-20261020-091200", profile: "supervisor", status: "idle", mode: "interactive", created_at: now - 1800, goal: "Sketch a CLI for tidying release notes" },
];

const goal: GoalDTO = {
  root_id: "sup-42",
  enabled: true,
  goal: "UX revamp of the brindle desktop app",
  detail: "Sessions tree as the backbone; goal and diff in the session view; inbox by session.",
  state: "running",
  goal_phase: "in_progress",
  pr_url: null,
  note: null,
  progress: 11,
  milestones: [
    { id: 1, position: 1, title: "Layout skeleton", status: "passed", state: "passed", state_why: null, check_cmd: "npm --prefix desktop run build", checked_at: now - 2400, output: "built in 1.9s" },
    { id: 2, position: 2, title: "Sessions screen", status: "passed", state: "passed", state_why: null, check_cmd: "npm --prefix desktop test -- --run src/screens/Sessions", checked_at: now - 1200, output: "Tests 3 passed" },
    { id: 3, position: 3, title: "Session view with goal and diff", status: "running", state: "not_done", state_why: "the goal panel's check hasn't run yet", check_cmd: "npm --prefix desktop test -- --run src/screens/Session", checked_at: null, output: null },
    { id: 4, position: 4, title: "Inbox by session", status: "pending", state: "regressed", state_why: "passed earlier; a later change broke it", check_cmd: "npm --prefix desktop test -- --run src/screens/Inbox", checked_at: now - 600, output: "1 test failed" },
    { id: 5, position: 5, title: "Full suite and build", status: "pending", state: "not_done", state_why: null, check_cmd: "npm --prefix desktop test -- --run && npm --prefix desktop run build", checked_at: null, output: null },
  ],
};

const inbox: InboxItemDTO[] = [
  // The waiting worker's prompt, as brindle's hook holds it (engine branch desktop/permission-items): answerable in place.
  {
    kind: "permission",
    reason: "asks permission",
    agent_id: "b31c9f02",
    workspace_id: "pawdelta/desktop-shots",
    root_id: "sup-42",
    detail: "Bash: npx -y playwright@1.49.1 install chromium",
    permission: {
      via: "hook",
      request_id: "toolu_01Playwright",
      digest: "9f2c4e",
      tool: "Bash",
      kind: "bash",
      target: "npx -y playwright@1.49.1 install chromium",
      wording: "Playwright wants to download Chromium 131 (161 MB) to ~/Library/Caches/ms-playwright.\nDo you want to proceed?",
      choices: [
        { choice: "allow_once", label: "Allow" },
        { choice: "allow_always", label: "Allow for npx here", rule: "Claude Code's own rule: Bash(npx -y playwright@1.49.1 install chromium:*)" },
        { choice: "deny", label: "Decline" },
      ],
    },
  },
  { kind: "review", reason: "to review", agent_id: "c58d1e77", workspace_id: "pawdelta/docs-readme", root_id: "sup-42" },
  { kind: "credentials", reason: "credentials need attention", agent_id: "e9a0b6c3", workspace_id: "pawdelta/fix-tray-badge", root_id: "sup-42", detail: "claude auth login", action: { method: "POST", path: "/v1/providers/claude/login" } },
  {
    kind: "plan",
    reason: "plan awaiting approval",
    agent_id: "a7aaa807",
    workspace_id: "pawdelta/desktop-ux-revamp",
    root_id: "sup-42",
    detail: "1. Move the session list into a shared feed (src/store/sessions.tsx).\n2. Sidebar: every session as a tree with its workers nested.\n3. Session view: goal panel above the tree, terminal | diff per agent.\n4. Inbox grouped by session, each item linking to its agent.\nTests: update App, Sessions and Inbox tests as the screens move.",
  },
  { kind: "proposed_checks", reason: "checks proposed", agent_id: "sup-42", workspace_id: "pawdelta", root_id: "sup-42", detail: "npm --prefix desktop test -- --run\nnpm --prefix desktop run build" },
  { kind: "org_notice", reason: "Reviewer profiles now default to reviewer-codex when Codex is installed.", detail: "admin", notice_id: "n-18" },
];

const DIFF = `diff --git a/desktop/src/components/Sidebar.tsx b/desktop/src/components/Sidebar.tsx
index 3f1c2aa..9b8e0d1 100644
--- a/desktop/src/components/Sidebar.tsx
+++ b/desktop/src/components/Sidebar.tsx
@@ -1,9 +1,12 @@
-/* The sidebar: the braid lockup, the app's places (Sessions, Inbox with its
-   "needs you" count), the open session's screens (its agents, goal and one
-   entry per branch its agents work on), and at the foot the engine
-   connection and the theme switch. */
+/* The sidebar, the app's backbone: the braid lockup, the two places
+   (Sessions, the home overview, and the Inbox with its "needs you" count),
+   then every session as a tree, grouped by repo: the session as the root
+   with its workers nested under it, each with its status at a glance and
+   the count of inbox items waiting on it. */

-import type { ReactNode } from "react";
+import { useMemo, type ReactNode } from "react";
+import { sessionTree, type AgentNode } from "../screens/Session/tree";
+import { DOT, agentState, isPaused, needsYou, tone } from "../store/overview";
 import { sameRoute, type Route } from "../routes";
@@ -40,6 +43,21 @@ function ConnectionBadge() {
   );
 }

+/* The count of inbox items waiting on an agent, as a badge. */
+export function NeedsYou({ count }: { count: number }) {
+  if (count === 0) return null;
+  return (
+    <span className="rounded-full bg-sap px-1.5 text-xs font-semibold leading-5 text-on-sap">
+      {count}
+    </span>
+  );
+}
+
diff --git a/desktop/src/store/overview.ts b/desktop/src/store/overview.ts
new file mode 100644
--- /dev/null
+++ b/desktop/src/store/overview.ts
@@ -0,0 +1,14 @@
+/* How a session and its agents read at a glance. */
+
+import type { AgentDTO, InboxItemDTO, SessionDTO } from "../api/schema";
+
+export const isPaused = (s: SessionDTO) => s.status === "paused" || s.status === "failed";
+export const isFailed = (s: SessionDTO) => s.status === "failed";
+
+export type Tone = "attention" | "busy" | "idle" | "off";
+
+export function tone(status: string, alive?: boolean): Tone {
+  if (alive === false) return "off";
+  if (status === "waiting" || status === "failed") return "attention";
+  return status === "processing" || status === "starting" ? "busy" : "idle";
+}
diff --git a/desktop/README.md b/desktop/README.md
index 7a1b2c3..8d4e5f6 100644
--- a/desktop/README.md
+++ b/desktop/README.md
@@ -3,8 +3,8 @@
 A Tauri 2 shell (React 19, TypeScript, Vite, Tailwind 4, web/'s palette and
 braid mark) over the local brindle daemon.

-A purple sidebar beside one screen at a time: Sessions (home) and the Inbox.
+A purple sidebar holding every session as a tree, beside one screen at a time.
Untracked files (2):
  desktop/shots/
  desktop/src/store/sessions.tsx
`;

const SHOTS_DIFF = `diff --git a/desktop/shots/README.md b/desktop/shots/README.md
new file mode 100644
--- /dev/null
+++ b/desktop/shots/README.md
@@ -0,0 +1,6 @@
+# Renderings
+
+A dev-only screenshot setup: a preview entry over the fake daemon with
+realistic fixtures, and a script that writes full-page PNGs, light and dark.
+
+    node shots/shots.mjs --step 01-skeleton
`;

/* desktop/README.md as it stands in docs/readme's worktree: what its diff's
   hunk shows, at lines 12-20, with the lines around it (an inline edit in
   the review sheet reads it, splices the hunk and writes it back). */
export const README_FILE = [
  "# brindle desktop app",
  "",
  "A Tauri 2 shell (React 19, TypeScript, Vite, Tailwind 4, web/'s palette and",
  "braid mark) over the local brindle daemon.",
  "",
  "Needs Node 20.19+ (22 in CI). Rust and the Tauri toolchain are needed only for",
  "`npm run tauri dev` / `tauri build`.",
  "",
  "```sh",
  "npm ci",
  "npm test              # Vitest (watch); npm test -- --run for one pass",
  "npm run build         # type-check and build the front end into dist/",
  "npm run tauri dev     # the app window, against `npm run dev`",
  "```",
  "",
  "Renderings of every screen, light and dark, come from `desktop/shots/`",
  "(see its README): a preview entry over the fake daemon.",
  "",
  "## Layout",
  "",
  "- `src/App.tsx`: the shell.",
  "",
].join("\n");

/* The README as the PR base has it: without the lines the branch added. */
export const README_BASE = README_FILE.replace("\nRenderings of every screen, light and dark, come from `desktop/shots/`\n(see its README): a preview entry over the fake daemon.\n", "");

export const README_DIFF = `diff --git a/desktop/README.md b/desktop/README.md
index 7a1b2c3..${fakeBlob(README_FILE).slice(0, 7)} 100644
--- a/desktop/README.md
+++ b/desktop/README.md
@@ -12,6 +12,9 @@ Needs Node 20.19+ (22 in CI).
 npm run build         # type-check and build the front end into dist/
 npm run tauri dev     # the app window, against \`npm run dev\`
 \`\`\`
+
+Renderings of every screen, light and dark, come from \`desktop/shots/\`
+(see its README): a preview entry over the fake daemon.
`;

const screen = (lines: string[]): Pane => ({ live: true, screen: lines.join("\n") + "\n", cursor: [2, lines.length] });

/* Each agent's pane as its provider's CLI draws it, distinct per agent and
   in the state its row shows: Claude Code's header box, ⏺ tool lines with
   ⎿ results, the input box and its status line; Codex's own turns. */
/* A box as the CLIs draw them, `inner` columns wide between its borders. */
const box = (lines: string[], inner = 76): string[] => ["╭" + "─".repeat(inner) + "╮", ...lines.map((l) => `│ ${[...l].length > inner - 2 ? [...l].slice(0, inner - 3).join("") + "…" : l + " ".repeat(inner - 2 - [...l].length)} │`), "╰" + "─".repeat(inner) + "╯"];
const CLAUDE_HEADER = (cwd: string, extra: string) => [...box(["✻ Welcome to Claude Code!", "", "  /help for help, /status for your current setup", `  cwd: ${cwd}`, `  ${extra}`]), ""];
const CLAUDE_INPUT = (status: string) => [...box([">"]), `  ⏵⏵ accept edits on (shift+tab to cycle)${" ".repeat(Math.max(1, 36 - [...status].length))}${status}`];

const terminals: Record<string, Pane> = {
  // The scratch session's chat, between turns.
  "scr-3": screen([
    ...CLAUDE_HEADER("~/.brindle/scratch/notes-20261020-091200", "model: claude-sonnet-4-5 · brindle scratch session"),
    "> Sketch a CLI that tidies release notes: one bullet per change, grouped by area.",
    "",
    "⏺ Wrote tidy_notes.py and a test; committed as \"tidy release notes: first cut\".",
    "",
    ...CLAUDE_INPUT(""),
  ]),
  // The supervisor, idle at its input between workers' reports (autopilot drives it).
  "sup-42": screen([
    ...CLAUDE_HEADER("~/Projects/pawdelta", "model: claude-sonnet-4-5 · brindle supervisor (autopilot on)"),
    "> Supervise: UX revamp of the brindle desktop app (5 milestones). Keep the store the one place state lives.",
    "",
    "⏺ brindle - check_milestone(milestone: 2)",
    "  ⎿  2 Sessions screen: PASSED (npm --prefix desktop test -- --run src/screens/Sessions · 3 passed) · 1 still passed",
    "",
    "⏺ brindle - assign(task: \"Session view: goal panel above the tree, terminal | diff per agent\", files: [\"desktop/src/screens/Session/**\"])",
    "  ⎿  started a7aaa807 on desktop/ux-revamp (developer, sonnet)",
    "",
    "⏺ brindle - assign(task: \"Renderings of every screen, light and dark\", files: [\"desktop/shots/**\"])",
    "  ⎿  started b31c9f02 on desktop/shots (developer, sonnet)",
    "",
    "⏺ c58d1e77 reported: docs/readme ready (2 commits, +48 −9). Its review is under way (reviewer-codex, d02f44a1).",
    "",
    "⏺ b31c9f02 is stopped at a permission prompt (Playwright wants Chromium); e9a0b6c3 stopped on a 401, its sign-in has lapsed.",
    "   Milestone 3 of 5 is in progress on desktop/ux-revamp. Waiting for the workers.",
    "",
    ...CLAUDE_INPUT("⧉ 118k tokens"),
  ]),
  // A worker at work: tool calls landing one after another (the demo keeps printing).
  a7aaa807: screen([
    ...CLAUDE_HEADER("~/Projects/pawdelta/.brindle/worktrees/desktop/ux-revamp", "model: claude-sonnet-4-5 · branch desktop/ux-revamp"),
    "> Session view: goal panel above the tree, terminal | diff per agent. Keep the store the one place state",
    "  lives (decision 7c1d9e2a). Tests: src/screens/Session.",
    "",
    "⏺ I'll read the current session screen first, then put the goal panel above the tree and key the diff by the",
    "  selected worker.",
    "",
    "⏺ Read(desktop/src/screens/Session/SessionScreen.tsx)",
    "  ⎿  Read 278 lines",
    "",
    "⏺ Read(desktop/src/screens/Session/tree.ts)",
    "  ⎿  Read 92 lines",
    "",
    "⏺ Update(desktop/src/screens/Session/SessionScreen.tsx)",
    "  ⎿  Updated desktop/src/screens/Session/SessionScreen.tsx with 61 additions and 14 removals",
    "       38    import { Terminal } from \"../../components/Terminal\";",
    "       39 +  import { GoalPanel } from \"../../components/GoalPanel\";",
    "       40 +  import { BranchPanel } from \"../../components/BranchPanel\";",
    "       96 -      <AgentList agents={agents} />",
    "       97 +      <GoalPanel goal={goal} />",
    "       98 +      <AgentTree nodes={sessionTree(agents)} selected={selected} onSelect={setSelected} />",
    "",
    "⏺ Bash(npm --prefix desktop test -- --run src/screens/Session)",
    "  ⎿   ✓ src/screens/Session/SessionScreen.test.tsx (6 tests) 412ms",
    "      Test Files  1 passed (1)",
    "      Tests  6 passed (6)",
    "",
    "✻ Working on the diff tab: the Branch panel keyed by the selected worker's workspace… (esc to interrupt)",
  ]),
  // A worker stopped at Claude Code's own permission prompt.
  b31c9f02: screen([
    ...CLAUDE_HEADER("~/Projects/pawdelta/.brindle/worktrees/desktop/shots", "model: claude-sonnet-4-5 · branch desktop/shots"),
    "> Renderings of every screen, light and dark: a preview entry over the fake daemon, screenshotted with",
    "  Playwright's CLI into desktop/shots/out/.",
    "",
    "⏺ Write(desktop/shots/shots.mjs)",
    "  ⎿  Wrote 74 lines to desktop/shots/shots.mjs",
    "",
    "⏺ Write(desktop/shots/preview.tsx)",
    "  ⎿  Wrote 58 lines to desktop/shots/preview.tsx",
    "",
    "⏺ Bash(npx -y playwright@1.49.1 install chromium)",
    "  ⎿  Playwright wants to download Chromium 131 (161 MB) to ~/Library/Caches/ms-playwright.",
    "",
    "  Do you want to proceed?",
    "  ❯ 1. Yes",
    "    2. Yes, and don't ask again for npx commands in ~/Projects/pawdelta/.brindle/worktrees/desktop/shots",
    "    3. No, and tell Claude what to do differently (esc)",
  ]),
  // A worker that finished and reported: its process has exited; the surface shows what it left (the pane tmux kept).
  c58d1e77: {
    live: false,
    text: [
      "⏺ Update(desktop/README.md)",
      "  ⎿  Updated desktop/README.md with 8 additions and 9 removals",
      "",
      "⏺ Bash(git commit -am \"Desktop: the shots setup documented\")",
      "  ⎿  [docs/readme 91ab2e4] Desktop: the shots setup documented",
      "      2 files changed, 48 insertions(+), 9 deletions(-)",
      "",
      "⏺ brindle - report_result(result: \"Documented the shots setup in desktop/shots/README.md and linked it from desktop/README.md…\")",
      "  ⎿  Reported. The supervisor has been told; a reviewer starts on docs/readme.",
      "",
      "⏺ Done: the shots setup is documented in desktop/shots/README.md (what it renders, how to run it) and linked from",
      "  desktop/README.md's Layout. Two commits on docs/readme, +48 −9, the suite green.",
      "",
    ].join("\n"),
  },
  // The Codex reviewer, finished: its verdict given, idle at its prompt.
  d02f44a1: screen([
    ...box([">_ OpenAI Codex (v0.48.0)", "", "model:  gpt-5-codex   /model to change", "cwd:    ~/Projects/pawdelta/.brindle/worktrees/docs/readme"], 62),
    "",
    "user",
    "Review branch docs/readme against dev/desktop-app: brindle's checks passed (npm --prefix desktop test -- --run:",
    "151 passed; npm --prefix desktop run build: ok). Approve only if it can merge as is.",
    "",
    "codex",
    "Reading the diff (2 files, +48 −9).",
    "",
    "• Ran git diff dev/desktop-app...docs/readme -- desktop/README.md desktop/shots/README.md",
    "  └ 57 lines",
    "",
    "codex",
    "Findings, most severe first:",
    "  1. (minor) desktop/shots/README.md says shots land in shots/out/<step>/ but shots.mjs defaults --out to",
    "     shots/out; the step is the subfolder. The text is right; only the example path lacks the step.",
    "  2. (nit) desktop/README.md's Layout bullet for shots/ could say Playwright is fetched by npx, not a dependency.",
    "Nothing blocks a merge.",
    "",
    "• Called brindle.submit_review(approved: true, summary: \"Docs only; one example path to tidy…\")",
    "  └ recorded",
    "",
    "codex",
    "Approved. The branch can merge as is.",
    "",
    "› ",
    "",
    "  ⏎ send   ⇧⏎ newline   ⌃T transcript   ⌃C quit                              gpt-5-codex · 23k tokens",
  ]),
  // A worker whose sign-in lapsed: Claude Code's own error, then nothing.
  e9a0b6c3: screen([
    ...CLAUDE_HEADER("~/Projects/pawdelta/.brindle/worktrees/fix/tray-badge", "model: claude-sonnet-4-5 · branch fix/tray-badge"),
    "> The tray badge counts only this machine's inbox items. Tests in src/screens/Inbox/notify.test.ts.",
    "",
    "⏺ Read(desktop/src/screens/Inbox/notify.ts)",
    "  ⎿  Read 66 lines",
    "",
    "⏺ Update(desktop/src/screens/Inbox/notify.ts)",
    "  ⎿  Updated desktop/src/screens/Inbox/notify.ts with 6 additions and 3 removals",
    "",
    "⏺ API Error: 401 {\"type\":\"error\",\"error\":{\"type\":\"authentication_error\",\"message\":\"OAuth token has expired.",
    "  Please obtain a new token or refresh your existing token.\"}} · Please run /login",
    "",
    ...CLAUDE_INPUT("⚠ not signed in"),
  ]),
};

/* The built-in profiles as GET /routing describes them on a Mac with Claude
   Code and Codex installed and signed in, Antigravity installed but signed
   out, no OpenCode, and Ollama (native) there. */
const rp = (name: string, provider: string, model: string | null, cost_tier: string, installed = true, signed_in = true): RoutingProfile => ({
  name,
  provider,
  model,
  cost_tier,
  installed,
  signed_in,
});
const routingProfiles: RoutingProfile[] = [
  rp("developer-light", "claude", "haiku", "low"),
  rp("developer", "claude", "sonnet", "medium"),
  rp("developer-heavy", "claude", "opus", "high"),
  rp("developer-fable", "claude", "fable", "high"),
  rp("developer-codex", "codex", "gpt-5-codex", "medium"),
  rp("developer-gemini", "antigravity", "gemini-3-pro", "high", true, false),
  rp("developer-opencode", "opencode", null, "medium", false, false),
  rp("developer-local", "native", "qwen3-coder:30b", "low"),
  rp("reviewer", "claude", "sonnet", "medium"),
  rp("reviewer-codex", "codex", "gpt-5-codex", "medium"),
  rp("reviewer-gemini", "antigravity", "gemini-3-pro", "high", true, false),
  rp("reviewer-local", "native", "qwen3-coder:30b", "low"),
  rp("qa", "claude", "sonnet", "medium"),
  rp("backend-auditor", "claude", "opus", "high"),
  rp("supervisor", "claude", null, "medium"),
  rp("coordinator", "claude", "haiku", "low"),
  rp("subagent", "subagent", null, "medium"),
];

/* Every profile as GET /profiles resolves it (brindle/service/dto.py ProfileDTO). */
const profile = (p: Partial<ProfileDTO> & Pick<ProfileDTO, "name" | "description">): ProfileDTO => ({
  provider: "claude",
  model: null,
  permission_mode: "acceptEdits",
  effort: null,
  headless: false,
  local: false,
  fallback: [],
  ...p,
});
const profiles: ProfileDTO[] = [
  profile({ name: "developer-light", description: "A developer on Haiku for small, well-specified work.", model: "haiku", effort: "low" }),
  profile({ name: "developer", description: "The default developer: implements a task on its own branch and reports.", model: "sonnet" }),
  profile({ name: "developer-heavy", description: "A developer on Opus for architecture and broad refactoring.", model: "opus", effort: "high" }),
  profile({ name: "developer-fable", description: "A developer on Fable for complex, long-running system design.", model: "fable", effort: "max" }),
  profile({ name: "developer-codex", description: "A developer on Codex.", provider: "codex", model: "gpt-5-codex", permission_mode: null }),
  profile({ name: "developer-gemini", description: "A developer on Google Antigravity.", provider: "antigravity", model: "gemini-3-pro", permission_mode: null }),
  profile({ name: "developer-opencode", description: "A developer on OpenCode.", provider: "opencode", permission_mode: null }),
  profile({ name: "developer-local", description: "A developer on a local model through Ollama.", provider: "native", model: "qwen3-coder:30b", local: true, fallback: ["developer-light"] }),
  profile({ name: "reviewer", description: "Reviews a branch against its task; approves or lists findings.", model: "sonnet", permission_mode: "plan" }),
  profile({ name: "reviewer-codex", description: "Reviews with Codex: a second model's eyes.", provider: "codex", model: "gpt-5-codex", permission_mode: null }),
  profile({ name: "reviewer-gemini", description: "Reviews with Google Antigravity.", provider: "antigravity", model: "gemini-3-pro", permission_mode: null }),
  profile({ name: "reviewer-local", description: "Reviews on a local model.", provider: "native", model: "qwen3-coder:30b", local: true }),
  profile({ name: "qa", description: "Writes and runs tests for a change; reports what fails.", model: "sonnet" }),
  profile({ name: "backend-auditor", description: "Audits a backend change for security and data-loss risks.", model: "opus", permission_mode: "plan" }),
  profile({ name: "supervisor", description: "The session's supervisor: plans, hands out work, reviews results.", permission_mode: null }),
  profile({ name: "coordinator", description: "Holds and orders queued work across sessions (brindle Pro).", model: "haiku", headless: true }),
  profile({ name: "subagent", description: "A task run in the supervisor's own Agent tool.", provider: "subagent", permission_mode: null }),
];

const decisions: DecisionDTO[] = [
  { id: "7c1d9e2a4b3f4c5d8e9f0a1b2c3d4e5f", repo_key: "pawdelta", topic: "state", body: "The store is the one place app state lives; screens read it with useAppState and never keep a copy of the snapshot.", rationale: "Two copies drift: the inbox count once disagreed with the list.", scope: ["desktop/src/**"], status: "accepted", created_at: now - 6 * 86400 },
  { id: "a91f3b7c2d4e4f6a9b8c7d6e5f4a3b2c", repo_key: "pawdelta", topic: "terminals", body: "Terminal bytes come from the daemon's control-mode stream; the app never runs tmux itself.", rationale: "One tmux client per session keeps CPU flat with six panes open.", scope: ["desktop/src/components/Terminal.tsx", "desktop/src/api/terminal.ts"], status: "accepted", created_at: now - 2 * 86400 },
  { id: "e2c4a6b8d0f24e6a8c0b2d4f6a8c0e2b", repo_key: "pawdelta", topic: "secrets", body: "No secret value ever reaches the webview: keys show as names and last four characters, tokens stay in the shell.", rationale: "The daemon's own rule (routes_read); the app repo is public.", scope: [], status: "accepted", created_at: now - 3600 * 5 },
];

/* The account's orgs, as GET /account lists them: the personal one and the team's. */
export const ORGS = [
  { id: "org_7f3a9c2e", name: "Sam", role: "owner", plan: "pro", personal: true, current: true },
  { id: "org_acme", name: "Acme Robotics", role: "member", plan: "team", personal: false, current: false },
];

export const data: Partial<DaemonData> = {
  repos: [
    { repo_root: REPO, name: "pawdelta" },
    { repo_root: OTHER, name: "brindle" },
    { repo_root: SCRATCH, name: "notes-20261020-091200" },
  ],
  sessions,
  workspaces,
  inbox,
  goal,
  profiles,
  decisions,
  // The workers' turn snapshots, which a rewind goes back to.
  turns: { a7aaa807: 4, b31c9f02: 2, c58d1e77: 6, e9a0b6c3: 1 },
  kbSources: [
    { id: "f0e1d2c3b4a5968778695a4b3c2d1e0f", kind: "folder", scope: `${REPO}/docs`, mode: "indexed", visibility: "team", added_at: now - 12 * 86400, docs: 14, quarantined: false },
    { id: "0a1b2c3d4e5f60718293a4b5c6d7e8f9", kind: "folder", scope: "/home/sam/Notes/pawdelta-design", mode: "indexed", visibility: "private", added_at: now - 86400, docs: 3, quarantined: true },
  ],
  profileDetails: {
    "developer-gemini": { source: "built-in profile developer-gemini", prompt: "Implement the task you are given on your own branch, as a developer on Google Antigravity would: read the code map first, keep the change focused, run the tests that cover it, commit and report.\n\nRules (your profile's rule packs). brindle checks the branch against the mechanical ones before it is reviewed or merged:\n\n## Rules: learned\nKeep each test to one behaviour; a new test must cover a new line.", rules: [{ name: "learned", description: "Rules accepted from review findings", source: `${REPO}/.brindle/rules/learned.md` }], lint: { errors: [], warnings: [] } },
    "developer-local": { source: "/home/sam/.brindle/profiles/developer-local.md", extends: ["developer"], prompt: "Implement the task on a local model; keep diffs small.", lint: { errors: [], warnings: ["add_dirs entry doesn't exist: /Volumes/data/fixtures"] } },
  },
  kbHits: [
    { chunk_id: "k1", heading_path: "Terminals > Control mode", text: "One tmux control-mode client per session streams every pane; the daemon fans the bytes out to each viewer and unpipes when the last one leaves.", url: `file://${REPO}/docs/terminals.md`, title: "Terminals", external_id: "docs/terminals.md", updated_at: now - 3 * 86400, updated: "2026-10-17" },
    { chunk_id: "k2", heading_path: "Design notes > Sidebar", text: "The sidebar is the backbone: every session as a tree with its workers nested, the same tree the Sessions screen shows in full.", url: "file:///home/sam/Notes/pawdelta-design/sidebar.md", title: "Design notes", external_id: "sidebar.md", updated_at: now - 200 * 86400, updated: "2026-04-03" },
  ],
  orgs: {},
  userSettings: { claude_permission_mode: "default" },
  config: {
    user: { supervisor: "claude", reviewer: "reviewer", sidebar: "left", routing_light: ["developer-light"] },
    repo: {
      checks: ["npm --prefix desktop test -- --run", "npm --prefix desktop run build"],
      autopilot: true,
      plan_first: false,
      overlap: null,
      stale_after: 30,
      coordinator: false,
      default_agent: "developer",
      reviewer: "reviewer-codex",
      exclude_providers: ["antigravity"],
      base_branch: "dev/desktop-app",
      review_rounds: 2,
      delegation: "balanced",
      protected_paths: ["desktop/package-lock.json"],
    },
    local: { max_agents: 4 },
    keys: [
      { name: "ANTHROPIC_API_KEY", set: true, last4: "k7Qa" },
      { name: "OPENAI_API_KEY", set: true, last4: "9xTz" },
      { name: "GEMINI_API_KEY", set: false, last4: "" },
    ],
  },
  doctor: [
    { level: "ok", name: "python", detail: "3.12.4" },
    { level: "ok", name: "tmux", detail: "/opt/homebrew/bin/tmux (tmux 3.5a)" },
    { level: "ok", name: "claude", detail: "/opt/homebrew/bin/claude (2.1.0)" },
    { level: "ok", name: "codex", detail: "/opt/homebrew/bin/codex (0.48.0)" },
    { level: "warn", name: "agy", detail: "not found: only needed for Google Antigravity agents" },
    { level: "warn", name: "opencode", detail: "not found: only needed for OpenCode agents (developer-opencode, reviewer-opencode)" },
    { level: "ok", name: "claude sign-in", detail: "signed in" },
    { level: "warn", name: "codex sign-in", detail: "signed out: run `codex login`; quota: no limit in effect" },
    { level: "warn", name: "antigravity sign-in", detail: "not signed in: run `agy` and sign in with your Google account" },
    { level: "ok", name: "gh", detail: "/opt/homebrew/bin/gh (2.62.0)" },
    { level: "warn", name: "pre-commit", detail: "not found: only needed if the repo uses pre-commit hooks" },
    { level: "ok", name: "clipboard", detail: "pbcopy: dragging in the chat copies to the clipboard" },
    { level: "ok", name: "daemon", detail: "running (pid 4242)" },
    { level: "ok", name: "brindle update", detail: "brindle 0.5.1 is available (you have 0.5.0): uv tool install --upgrade brindle" },
  ],
  account: {
    signed_in: true,
    name: "sam@example.com",
    plan: "pro",
    org_id: "org_7f3a9c2e",
    status: "active",
    features: ["ci_fix", "coordinator", "cost", "federation", "guardrails", "learned_rules", "learning", "multi_repo", "services", "settings_sync"],
    seats: 1,
    role: "owner",
    expires_at: now + 23 * 86400,
    in_grace: false,
    grace_until: null,
    learning: true,
    learning_note: "not enough similar tasks yet: kept the default for the last pick",
    orgs: ORGS,
  },
  // A Team org's policy: Claude Code and Codex only, four models, the Claude route managed by the org, no personal keys.
  routingPolicy: {
    org_id: "org_acme",
    allowed_providers: ["claude", "codex"],
    allowed_models: ["opus", "sonnet", "haiku", "gpt-5-codex"],
    managed_routes: ["claude", "codex"],
    deny_personal_keys: true,
  },
  // What each action terminal shows as it starts.
  actionScreens: {
    "login antigravity": "$ agy\n\nGoogle Antigravity\n\nOpening your browser to sign in with your Google account…\nIf it didn't open, visit: https://accounts.google.com/o/oauth2/auth?client_id=agy&state=k3q9\n\nWaiting for the browser…\n",
    "login claude": "$ claude auth login\n\nOpening https://claude.ai/oauth/authorize?client_id=claude-code&state=7f1c in your browser…\nPaste the code here once you have signed in:\n> ",
    "login codex": "$ codex login\n\nOpening https://auth.openai.com/oauth/authorize?client_id=codex&state=2b9e in your browser…\nIf it didn't open, visit the link above.\n\nWaiting for the browser…\n",
    "key OPENAI_API_KEY": "$ brindle keys set OPENAI_API_KEY\nValue for OPENAI_API_KEY (hidden): ",
    "key GEMINI_API_KEY": "$ brindle keys set GEMINI_API_KEY\nValue for GEMINI_API_KEY (hidden): ",
    upgrade: "$ uv tool upgrade brindle\nResolved 24 packages in 412ms\nPrepared 1 package in 1.3s\nUninstalled 1 package in 18ms\nInstalled 1 package in 22ms\n - brindle==0.5.0\n + brindle==0.5.1\nInstalled 1 executable: brindle\n",
  },
  costEntitled: true,
  cost: {
    days: 30,
    total: { dollars: 184.37, tokens: 42_600_000, unknown: 0, free: 3_100_000 },
    by_model: {
      sonnet: { dollars: 96.2, tokens: 24_100_000, unknown: 0, free: 0 },
      opus: { dollars: 71.9, tokens: 6_300_000, unknown: 0, free: 0 },
      "gpt-5-codex": { dollars: 14.1, tokens: 8_200_000, unknown: 0, free: 0 },
      haiku: { dollars: 2.17, tokens: 900_000, unknown: 0, free: 0 },
      "qwen3-coder:30b": { dollars: 0, tokens: 3_100_000, unknown: 0, free: 3_100_000 },
    },
  },
  terminals,
  routingProfiles,
  // The team keeps Gemini out and reviews with Codex; this person put Opus first for heavy work.
  routingConfig: {
    user: {},
    repo: { exclude_providers: ["antigravity"], reviewer: "reviewer-codex" },
    local: { routing: { heavy: ["developer-heavy", "developer-fable", "developer-codex"] } },
  },
  diffs: {
    "pawdelta/desktop-ux-revamp": { workspace_id: "pawdelta/desktop-ux-revamp", base: "dev/desktop-app", diff: DIFF, stat: "" },
    "pawdelta/desktop-shots": { workspace_id: "pawdelta/desktop-shots", base: "dev/desktop-app", diff: SHOTS_DIFF, stat: "" },
    "pawdelta/docs-readme": { workspace_id: "pawdelta/docs-readme", base: "dev/desktop-app", diff: README_DIFF, stat: "" },
  },
  files: { "pawdelta/docs-readme": { "desktop/README.md": README_FILE } },
  // Codex asks once per folder before it works there: a start on it shows the item, not the CLI's prompt.
  preflight: { codex: [{ id: "trust", kind: "trust", label: `Trust the folder ${REPO}`, detail: "Codex asks once per folder before it works there.", done: false }] },
};
