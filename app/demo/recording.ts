/* The README's recording (scripts/record_app_demo.sh opens the demo with
   ?record): the demo's afternoon made calm and true to the engine, for the
   GIF only. The interactive demo and the renderings' fixtures are left as
   they are.

   - A calm inbox: a permission to answer and a plan to approve, then the PR
     brindle prepared (the badge counts what the Inbox lists, frame by frame).
   - One live session with three workers, each in a state its pane, its row
     and the milestones agree on: one at work, one at a permission prompt,
     one plan-first worker waiting on its plan. No failed worker, no review
     already given, no other repos.
   - The panes as Claude Code 2.x draws them (its logo header and model,
     transcript, spinner, input box between rules, its own permission
     dialog), redrawn whole as the CLI does, and the tool results in the
     engine's own words (assign's, report_result's, merge_workspace's).
   - The files the workers read, write and edit are recordingFiles.ts's: the
     panes' counts and the PR's diff are both made from them. */

import type { InboxItemDTO } from "../src/api/schema";
import { unifiedDiff, type FakeDaemon } from "../src/test/fakeDaemon";
import { usageScenario } from "../src/test/usage";
import { CHANGES } from "./recordingFiles";

export const RECORDING = new URLSearchParams(location.search).has("record");

const WT = "~/Projects/pawdelta/.brindle/worktrees";
const SESSION = "desktop/src/screens/Session/SessionScreen.tsx";
const SESSION_TEST = "desktop/src/screens/Session/SessionScreen.test.tsx";
const INBOX = "desktop/src/screens/Inbox";
const PLAN = [
  "1. Group the Inbox's cards by session, newest first (new src/screens/Inbox/group.ts, used by InboxScreen.tsx).",
  "2. Each card links to its agent in the session view.",
  "3. The tray badge counts the same items the Inbox lists (src/screens/Inbox/notify.ts).",
  "Tests: src/screens/Inbox/notify.test.ts.",
].join("\n");

/* A pane as Claude Code 2.x draws it. */
interface Claude {
  cwd: string;
  model: string;
  blocks: string[][];
  /* A turn running: the spinner's verb, when it began, its tokens so far. */
  busy: { verb: string; since: number; tokens: number } | null;
  /* Its permission dialog, in the input box's place. */
  dialog: string[] | null;
}

/* Claude Code's tool lines for the files the workers change (recordingFiles.ts), every count made from them. */
const lineCount = (text: string) => text.split("\n").length - (text.endsWith("\n") ? 1 : 0);
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
const hunkLines = (path: string) => {
  const diff = unifiedDiff(path, CHANGES[path].before, CHANGES[path].after).split("\n");
  return diff.slice(diff.findIndex((l) => l.startsWith("@@")));
};
function stat(paths: string[]): { added: number; removed: number } {
  const lines = paths.flatMap(hunkLines).filter((l) => !l.startsWith("@@"));
  return { added: lines.filter((l) => l.startsWith("+")).length, removed: lines.filter((l) => l.startsWith("-")).length };
}
const read = (path: string) => [`⏺ Read(${path})`, `  ⎿  Read ${lineCount(CHANGES[path].before ?? CHANGES[path].after)} lines (ctrl+o to expand)`];
const write = (path: string) => [`⏺ Write(${path})`, `  ⎿  Wrote ${lineCount(CHANGES[path].after)} lines to ${path}`];
/* An edit as Claude Code shows it: its counts, then the first hunk with its line numbers (`shown` lines of it). */
function update(path: string, shown = 0): string[] {
  const { added, removed } = stat([path]);
  const out = [`⏺ Update(${path})`, `  ⎿  Updated ${path} with ${plural(added, "addition")}${removed ? ` and ${plural(removed, "removal")}` : ""}`];
  const lines = hunkLines(path);
  const end = lines.findIndex((l, i) => i > 0 && l.startsWith("@@"));
  // Each run of changed lines with its removals first, as Claude Code (and git) shows them.
  const hunk = lines
    .slice(1, end < 0 ? undefined : end)
    .filter((l) => l !== "")
    .reduce<string[][]>((runs, l) => {
      const last = runs.at(-1);
      if (last && (l[0] === " ") === (last[0][0] === " ")) last.push(l);
      else runs.push([l]);
      return runs;
    }, [])
    .flatMap((run) => (run[0][0] === " " ? run : [...run.filter((l) => l[0] === "-"), ...run.filter((l) => l[0] === "+")]));
  const head = lines[0].match(/^@@ -(\d+),\d+ \+(\d+),\d+ @@/)!;
  let [old, now] = [Number(head[1]), Number(head[2])];
  for (const l of hunk.slice(0, shown)) {
    const n = l[0] === "-" ? old++ : now++;
    if (l[0] === " ") old++;
    out.push(`${String(n).padStart(9)} ${l[0]}  ${l.slice(1)}`);
  }
  if (shown && hunk.length > shown) out.push(`       … +${plural(hunk.length - shown, "line")} (ctrl+o to expand)`);
  return out;
}
const testsIn = (path: string) => (CHANGES[path].after.match(/^\s+it\(/gm) ?? []).length;
const BRANCH_FILES = (branch: string) => Object.keys(CHANGES).filter((p) => CHANGES[p].branch === branch);

const SPIN = ["·", "✢", "✳", "✶", "✻", "✽"];
const age = (s: number) => (s < 60 ? `${Math.floor(s)}s` : `${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`);

function draw(c: Claude, cols: number, rows: number, now: number): { lines: string[]; cursor: [number, number] } {
  const rule = "─".repeat(Math.max(20, cols - 1));
  const out = [" ▐▛███▜▌   Claude Code v2.1.0", `▝▜█████▛▘  ${c.model} · Claude Max`, `  ▘▘ ▝▝    ${c.cwd}`, ""];
  for (const b of c.blocks) out.push(...b, "");
  if (c.dialog) out.push(rule, ...c.dialog);
  else {
    if (c.busy) {
      const s = (now - c.busy.since) / 1000;
      out.push(`${SPIN[Math.floor(now / 250) % SPIN.length]} ${c.busy.verb}… (${age(s)} · ↓ ${(c.busy.tokens + s * 0.04).toFixed(1)}k tokens · esc to interrupt)`, "");
    }
    out.push(rule, "❯ ", rule, "  ⏵⏵ accept edits on (shift+tab to cycle)");
  }
  // tmux shows the last screenful; the header scrolls away first.
  const lines = out.slice(Math.max(0, out.length - rows));
  const prompt = lines.lastIndexOf("❯ ");
  return { lines, cursor: prompt >= 0 ? [2, prompt] : [0, lines.length - 1] };
}

export function startRecording(daemon: FakeDaemon): void {
  const d = daemon.data;
  const t0 = Date.now();
  const now = Date.now() / 1000;

  /* The session: sup-42 and three workers; the paused session of the same repo folded under it. */
  d.repos = (d.repos ?? []).filter((r) => r.name === "pawdelta");
  d.sessions = (d.sessions ?? []).filter((s) => s.id === "sup-42" || s.id === "sup-39");
  d.workspaces = (d.workspaces ?? [])
    .filter((ws) => ["pawdelta", "pawdelta/desktop-ux-revamp", "pawdelta/desktop-shots", "pawdelta/fix-tray-badge"].includes(ws.id))
    .map((ws) =>
      ws.id === "pawdelta/fix-tray-badge"
        ? {
            ...ws,
            id: "pawdelta/desktop-inbox",
            name: "desktop-inbox",
            branch: "desktop/inbox",
            path: `${d.repos![0].repo_root}/.brindle/worktrees/desktop/inbox`,
            ahead: 0,
            agents: ws.agents?.map((a) => ({ ...a, status: "idle", status_since: now - 70 })),
          }
        : ws,
    )
    .map((ws) => ({ ...ws, agents: ws.agents?.map((a) => ({ ...a, profile: a.mode === "worker" ? "developer" : "supervisor", subagents: [] })) }));

  /* The milestones as the workers stand: two passed, the session view in progress, the rest not yet checked. */
  if (d.goal) {
    d.goal = {
      ...d.goal,
      milestones: d.goal.milestones?.map((m) =>
        m.position <= 2 ? m : { ...m, status: m.position === 3 ? "running" : "pending", state: "not_done", state_why: null, checked_at: null, output: null },
      ),
    };
  }

  /* The calm inbox: the shots worker's permission and the inbox worker's plan, nothing else. */
  const permission = d.inbox!.find((i) => i.kind === "permission" && i.agent_id === "b31c9f02")!;
  const plan: InboxItemDTO = { kind: "plan", reason: "plan awaiting approval", agent_id: "e9a0b6c3", workspace_id: "pawdelta/desktop-inbox", root_id: "sup-42", detail: PLAN };
  d.inbox = [permission, plan];

  /* The panes. */
  const panes: Record<string, Claude> = {
    "sup-42": {
      cwd: "~/Projects/pawdelta",
      model: "Opus 5.5",
      busy: null,
      dialog: null,
      blocks: [
        ["> Supervise: UX revamp of the brindle desktop app (5 milestones). Keep the store the one place state lives."],
        ["⏺ brindle - get_progress (MCP)", "  ⎿  Goal progress. Goal: UX revamp of the brindle desktop app", "     2 of 5 milestones verified.", "     … +6 lines (ctrl+o to expand)"],
        [
          "⏺ brindle - assign (MCP)(task: \"Session view: goal panel above the tree, terminal | diff per agent\", files: [\"desktop/src/screens/Session/**\"])",
          "  ⎿  Started worker a7aaa807 (developer) in workspace pawdelta/desktop-ux-revamp on branch desktop/ux-revamp.",
        ],
        [
          "⏺ brindle - assign (MCP)(task: \"Renderings of every screen, light and dark\", files: [\"desktop/shots/**\"])",
          "  ⎿  Started worker b31c9f02 (developer) in workspace pawdelta/desktop-shots on branch desktop/shots.",
        ],
        [
          "⏺ brindle - assign (MCP)(task: \"Inbox by session\", files: [\"desktop/src/screens/Inbox/**\"], plan_first: true)",
          "  ⎿  Started worker e9a0b6c3 (developer) in workspace pawdelta/desktop-inbox on branch desktop/inbox.",
        ],
        ["⏺ Three workers out: the session view, the renderings, and the inbox (plan first). I'll check the milestones as their", "  branches merge."],
      ],
    },
    a7aaa807: {
      cwd: `${WT}/desktop/ux-revamp`,
      model: "Sonnet 5.5",
      busy: { verb: "Marinating", since: t0 - 95_000, tokens: 6.1 },
      dialog: null,
      blocks: [
        ["> Session view: goal panel above the tree, terminal | diff per agent. Keep the store the one place state", "  lives (decision 7c1d9e2a). Tests: src/screens/Session."],
        ["⏺ I'll read the current session screen first, then put the goal panel above the tree and key the diff by the", "  selected worker."],
        read(SESSION),
        update(SESSION, 10),
      ],
    },
    b31c9f02: {
      cwd: `${WT}/desktop/shots`,
      model: "Sonnet 5.5",
      busy: null,
      blocks: [
        ["> Renderings of every screen, light and dark: a preview entry over the fake daemon, screenshotted with", "  Playwright into desktop/shots/out/."],
        write("desktop/shots/shots.mjs"),
        write("desktop/shots/preview.tsx"),
      ],
      dialog: [
        " Bash command",
        "",
        "   npx -y playwright@1.49.1 install chromium",
        "   Download Chromium for Playwright's screenshots",
        "",
        " Do you want to proceed?",
        " ❯ 1. Yes",
        "   2. Yes, and don't ask again for npx -y playwright@1.49.1 install chromium commands in",
        `      ${WT}/desktop/shots`,
        "   3. No, and tell Claude what to do differently (esc)",
      ],
    },
    e9a0b6c3: {
      cwd: `${WT}/desktop/inbox`,
      model: "Sonnet 5.5",
      busy: null,
      dialog: null,
      blocks: [
        ["> Inbox by session: group the cards by session, each linking to its agent. Plan first: send the plan with", "  submit_plan and wait for the decision before editing."],
        read(`${INBOX}/InboxScreen.tsx`),
        read(`${INBOX}/notify.ts`),
        [
          "⏺ brindle - submit_plan (MCP)(plan: \"1. Group the Inbox's cards by session, newest first…\")",
          "  ⎿  Plan sent. Stop now and wait for your supervisor's decision; don't edit files until it's approved.",
        ],
        ["⏺ The plan is sent; I'll wait for the decision before touching any file."],
      ],
    },
  };

  const size = (id: string): [number, number] => {
    const view = daemon.viewers(id).at(-1);
    return view?.resizes.at(-1) ?? (view ? [view.size.cols, view.size.rows] : [140, 54]);
  };
  const drawnAt: Record<string, string> = {};
  const redraw = (id: string) => {
    const [cols, rows] = size(id);
    drawnAt[id] = `${cols}x${rows}`;
    const { lines, cursor } = draw(panes[id], cols, rows, Date.now());
    d.terminals = { ...d.terminals, [id]: { live: true, screen: lines.join("\n") + "\n", cursor } };
    daemon.print(id, `\x1b[H\x1b[2J${lines.join("\r\n")}\x1b[${cursor[1] + 1};${cursor[0] + 1}H`);
  };
  const redrawAll = () => Object.keys(panes).forEach(redraw);
  redrawAll();
  // The spinners tick, as the CLI animates them, and a pane whose window was resized redraws at its new width.
  setInterval(() => Object.keys(panes).forEach((id) => (panes[id].busy || drawnAt[id] !== size(id).join("x")) && redraw(id)), 250);

  const status = (id: string, value: string) => {
    d.workspaces = d.workspaces!.map((ws) => ({ ...ws, agents: ws.agents?.map((a) => (a.id === id ? { ...a, status: value, status_since: Date.now() / 1000 } : a)) }));
    daemon.publish("agent.status", { agent_id: id, payload: { status: value } });
  };
  const at = (ms: number, step: () => void) => setTimeout(() => (step(), redrawAll()), Math.max(0, ms - (Date.now() - t0)));
  const after = (ms: number, step: () => void) => setTimeout(() => (step(), redrawAll()), ms);

  /* The session-view worker goes on: a tool call every few seconds. */
  const ux = panes.a7aaa807;
  at(4_000, () => ux.blocks.push(["⏺ Read(desktop/src/screens/Branch/DiffView.tsx)", "  ⎿  Read 246 lines (ctrl+o to expand)"]));
  at(9_000, () => ux.blocks.push(update(SESSION_TEST)));
  const tests = testsIn(SESSION_TEST);
  at(14_000, () => ux.blocks.push(["⏺ Bash(npm --prefix desktop test -- --run src/screens/Session)", `  ⎿   ✓ src/screens/Session/SessionScreen.test.tsx (${plural(tests, "test")}) 1.1s`, `      Tests  ${tests} passed (${tests})`]));
  at(17_500, () => {
    const files = BRANCH_FILES("desktop/ux-revamp");
    const { added, removed } = stat(files);
    ux.blocks.push(["⏺ Bash(git commit -am \"Session view: goal panel above the tree, terminal | diff per agent\")", "  ⎿  [desktop/ux-revamp 4f2a9c1] Session view: goal panel above the tree, terminal | diff per agent", `      ${files.length} files changed, ${added} insertions(+), ${removed} deletions(-)`]);
  });

  /* The shots worker: allowed, its download runs and it goes back to its task. */
  const shots = panes.b31c9f02;
  let answered = 0;
  const watch = setInterval(() => {
    const a = daemon.permissionAnswers[answered];
    if (!a) return;
    answered++;
    clearInterval(watch);
    shots.dialog = null;
    if (a.choice === "deny") {
      shots.blocks.push(["⏺ Bash(npx -y playwright@1.49.1 install chromium)", "  ⎿  Error: The user doesn't want to proceed with this tool use. The tool use was rejected."]);
      redrawAll();
      return;
    }
    const run = ["⏺ Bash(npx -y playwright@1.49.1 install chromium)", "  ⎿  Running…"];
    shots.blocks.push(run);
    shots.busy = { verb: "Rendering", since: Date.now(), tokens: 0.4 };
    redrawAll();
    after(2_500, () => {
      run.splice(1, 1, "  ⎿  Downloading Chromium 131.0.6778.33 (playwright build v1148)", "     161.3 MiB [====================] 100% 0.0s", "     Chromium 131.0.6778.33 (playwright build v1148) downloaded to ~/Library/Caches/ms-playwright/chromium-1148");
    });
    // shots.mjs prints each file it writes: every scene asked for, light then dark.
    after(4_500, () => shots.blocks.push(["⏺ Bash(node desktop/shots/shots.mjs --scene session,inbox)", "  ⎿  desktop/shots/out/session-light.png", "     desktop/shots/out/inbox-light.png", "     desktop/shots/out/session-dark.png", "     desktop/shots/out/inbox-dark.png"]));
    after(6_000, () => shots.blocks.push(write("desktop/shots/README.md")));
    after(7_000, () => shots.blocks.push(update("desktop/README.md")));
  }, 200);

  /* The inbox worker: its plan approved, the message brindle types into its chat, and it starts. */
  const inbox = panes.e9a0b6c3;
  const planned = setInterval(() => {
    if (d.inbox!.some((i) => i.kind === "plan" && i.agent_id === "e9a0b6c3")) return;
    clearInterval(planned);
    inbox.blocks.push(["> brindle delivered this message:", "  Your plan is approved: go ahead and implement it."]);
    inbox.busy = { verb: "Grouping", since: Date.now(), tokens: 0.2 };
    status("e9a0b6c3", "processing");
    redrawAll();
    after(2_000, () => inbox.blocks.push(write(`${INBOX}/group.ts`)));
    after(3_500, () => inbox.blocks.push(update(`${INBOX}/InboxScreen.tsx`)));
    after(5_000, () => inbox.blocks.push(update(`${INBOX}/notify.ts`)));
    after(6_500, () => inbox.blocks.push(update(`${INBOX}/notify.test.ts`)));
  }, 200);

  /* At twenty seconds (the demo's lifecycle): each worker reports, the supervisor merges their branches and checks the milestones. */
  at(19_800, () => {
    for (const p of [ux, shots, inbox]) {
      p.busy = null;
      p.dialog = null;
      p.blocks.push([
        "⏺ brindle - report_result (MCP)(result: \"Done; tests pass…\")",
        "  ⎿  result recorded. brindle is having your branch reviewed; it will merge it, or send you the review's findings to fix.",
      ]);
    }
    panes["sup-42"].blocks.push(
      ...["desktop/ux-revamp", "desktop/shots", "desktop/inbox"].map((branch, i) => [
        `⏺ brindle - merge_workspace (MCP)(workspace: "pawdelta/${branch.replace("/", "-")}")`,
        `  ⎿  Merged ${branch} into dev/desktop-app at ${["8d3b6f0", "2a71c4e", "f05e9b3"][i]} (review approved ${["4f2a9c1d", "b71e03d6", "9c58a2e4"][i]}; \`npm --prefix desktop test -- --run\` passed).`,
      ]),
      ["⏺ brindle - check_milestone (MCP)", "  ⎿  Goal progress. Goal: UX revamp of the brindle desktop app", "     5 of 5 milestones verified.", "     … +7 lines (ctrl+o to expand)"],
      ["⏺ Every milestone's check passes on dev/desktop-app: the goal is reached. brindle is preparing the pull request."],
    );
  });
}

/* The session's files with its three workers' merged work in them (sessionFiles, sessionBase:
   recordingFiles.ts, which the panes' counts come from too), and its diff against main as git would print it. */
export function mergedSession(files: Record<string, string>, base: Record<string, string>): { files: Record<string, string>; base: Record<string, string>; diff: string } {
  const now = { ...files };
  const was = { ...base };
  for (const [path, { before, after }] of Object.entries(CHANGES)) {
    now[path] = after;
    if (before !== null) was[path] = before;
    else delete was[path];
  }
  const diff = [...new Set([...Object.keys(was), ...Object.keys(now)])].sort().map((p) => unifiedDiff(p, was[p] ?? null, now[p] ?? null)).join("");
  return { files: now, base: was, diff };
}

/* Usage as it reads with only Claude Code at work: well within its limits, no other provider's reading in its place. */
export function recordingUsage(): ReturnType<typeof usageScenario> {
  const usage = usageScenario("normal");
  return { ...usage, providers: usage.providers.filter((p) => p.provider === "claude"), near: [] };
}

/* The workers once the milestones pass (the demo's lifecycle at twenty seconds): reported and merged, so their rows read done. */
export function mergedWorkers(daemon: FakeDaemon): void {
  daemon.data.workspaces = daemon.data.workspaces!.map((ws) => (ws.base_branch ? { ...ws, ahead: 0, behind: 0, dirty: 0, review: "approved" } : ws));
}
