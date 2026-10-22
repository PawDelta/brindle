/* Shapes as the daemon serves them (brindle ls --json, through service/dto.py). */

import type { InboxItemDTO, WorkspaceDTO } from "../api/schema";

export const REPO = "/home/u/proj";

/* The tmux window the engine names for an agent, on brindle's server ("brindle"): where the app's tmux client attaches. */
export const windowOf = (agent: string) => `brindle-sup1:${agent}`;

/* The workspaces with each agent's window named (one an agent says itself, null included, stands). */
export const windowed = (workspaces: WorkspaceDTO[]): WorkspaceDTO[] =>
  workspaces.map((w) => ({ ...w, agents: w.agents?.map((a) => ({ window: windowOf(a.id), tmux_server: "brindle", ...a })) }));

export const root: WorkspaceDTO = {
  id: "proj",
  name: "proj",
  branch: "main",
  base_branch: null,
  path: REPO,
  ahead: 0,
  behind: 0,
  dirty: 0,
  agents: [
    { id: "sup1", profile: "claude", provider: "claude", status: "idle", mode: "supervisor", status_since: 100, subagents: [], window: windowOf("sup1"), tmux_server: "brindle" },
  ],
  review: null,
  review_state: null,
  autopilot: true,
  asking: null,
};

export const worker: WorkspaceDTO = {
  id: "proj/feat-ls",
  name: "feat-ls",
  branch: "feat/ls",
  base_branch: "main",
  path: `${REPO}/.brindle/worktrees/feat/ls`,
  ahead: 2,
  behind: 0,
  dirty: 1,
  agents: [
    {
      id: "w1",
      profile: "claude",
      provider: "claude",
      status: "processing",
      mode: "worker",
      parent_id: "sup1",
      status_since: 120,
      subagents: [],
      window: windowOf("w1"),
      tmux_server: "brindle",
    },
  ],
  review: null,
  review_state: "pending",
  autopilot: false,
  asking: null,
};

export const question: InboxItemDTO = {
  kind: "question",
  reason: "Which database should the cache use?",
  agent_id: "sup1",
  workspace_id: "proj",
  detail: null,
};

export const waiting: InboxItemDTO = {
  kind: "waiting",
  reason: "waiting for input",
  agent_id: "w1",
  workspace_id: "proj/feat-ls",
  detail: null,
};

/* A permission request brindle's hook holds (engine branch desktop/permission-items):
   answerable from the app with the choices it offers. */
export const permission: InboxItemDTO = {
  kind: "permission",
  reason: "asks permission",
  agent_id: "w1",
  workspace_id: "proj/feat-ls",
  root_id: "sup1",
  detail: "Bash: npx playwright install chromium",
  permission: {
    via: "hook",
    request_id: "req-1",
    digest: "d1",
    tool: "Bash",
    kind: "bash",
    target: "npx playwright install chromium",
    choices: [
      { choice: "allow_once", label: "Allow once" },
      { choice: "allow_always", label: "Allow always", rule: "brindle's permission rule: allow bash exact 'npx playwright install chromium'" },
      { choice: "deny", label: "Deny" },
    ],
  },
};

/* A prompt on the CLI's own screen that brindle can't answer by keys: only its terminal can. */
export const permissionInTerminal: InboxItemDTO = {
  kind: "permission",
  reason: "asks permission",
  agent_id: "sup1",
  workspace_id: "proj",
  root_id: "sup1",
  detail: "Allow this command?",
  permission: { via: "prompt", request_id: "req-2", wording: "exec (needs approval)\n$ uv run alembic upgrade head\nAllow this command?", terminal: "its prompt doesn't show which option is selected", choices: [] },
};
