/* A session's agents as a tree: its root (the supervisor) and every agent
   whose parent chain leads back to it, across the session's workspaces.

   The snapshot leaves out a closed agent but keeps the agents it started that
   still run (view.snapshot): those hang under their nearest shown ancestor,
   found through `hidden`, each left-out agent's own parent (GET /agents/{id}
   still serves a closed agent's record). */

import type { AgentDTO, WorkspaceDTO } from "../../api/schema";

export interface AgentNode {
  agent: AgentDTO;
  workspace: WorkspaceDTO;
  children: AgentNode[];
}

/* A left-out agent's id → its parent's id (null: it has none, or no record is left). */
export type Hidden = Record<string, string | null>;

type Entry = { agent: AgentDTO; workspace: WorkspaceDTO };

const entries = (workspaces: WorkspaceDTO[]): Entry[] =>
  workspaces.flatMap((workspace) => (workspace.agents ?? []).map((agent) => ({ agent, workspace })));

/* Up `agent`'s parent chain: its nearest ancestor in the snapshot (`shown`),
   or the left-out ancestor whose parent isn't known yet (`unknown`). */
function climb(agent: AgentDTO, shown: Set<string>, hidden: Hidden): { shown?: string; unknown?: string } {
  const seen = new Set<string>();
  let p = agent.parent_id ?? null;
  while (p !== null && !seen.has(p)) {
    if (shown.has(p)) return { shown: p };
    if (!(p in hidden)) return { unknown: p };
    seen.add(p);
    p = hidden[p];
  }
  return {};
}

export function sessionTree(workspaces: WorkspaceDTO[], root: string, hidden: Hidden = {}): AgentNode | null {
  const all = entries(workspaces);
  const shown = new Set(all.map((e) => e.agent.id));
  const parent = new Map(all.map((e) => [e.agent.id, climb(e.agent, shown, hidden).shown]));
  const seen = new Set<string>();
  const build = (entry: Entry): AgentNode => {
    seen.add(entry.agent.id);
    return {
      ...entry,
      children: all.filter((e) => parent.get(e.agent.id) === entry.agent.id && !seen.has(e.agent.id)).map(build),
    };
  };
  const top = all.find((e) => e.agent.id === root);
  return top ? build(top) : null;
}

/* Left-out ancestors whose own parent has to be looked up before the tree is whole. */
export function unknownAncestors(workspaces: WorkspaceDTO[], hidden: Hidden): string[] {
  const all = entries(workspaces);
  const shown = new Set(all.map((e) => e.agent.id));
  const out = new Set<string>();
  for (const { agent } of all) {
    const { unknown } = climb(agent, shown, hidden);
    if (unknown) out.add(unknown);
  }
  return [...out];
}

export function agentIds(node: AgentNode | null): Set<string> {
  const ids = new Set<string>();
  const walk = (n: AgentNode) => {
    ids.add(n.agent.id);
    n.children.forEach(walk);
  };
  if (node) walk(node);
  return ids;
}

/* "12s", "4m", "3h", "2d" since `ts` (seconds). */
export function since(ts: number | null | undefined, now = Date.now() / 1000): string | null {
  if (ts == null) return null;
  const s = Math.max(0, Math.round(now - ts));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

/* The repo a workspace belongs to: the known repo whose root holds its path
   (the deepest, for a repo inside another), else the one its id is named
   for ("proj/feat-ls"), else `fallback`. A worker of a session can work in
   an attached repo, not the session's own. */
export function repoOfWorkspace(ws: WorkspaceDTO, repos: { repo_root: string; name: string }[], fallback: string): string {
  const holds = repos
    .filter((r) => ws.path === r.repo_root || ws.path.startsWith(`${r.repo_root.replace(/\/+$/, "")}/`))
    .sort((a, b) => b.repo_root.length - a.repo_root.length);
  if (holds.length > 0) return holds[0].repo_root;
  return repos.find((r) => r.name === ws.id.split("/")[0])?.repo_root ?? fallback;
}

/* A session's agents and workspaces from the snapshot, whether or not its
   root is in it (a paused session's isn't, so it has no tree): an agent is
   the session's when its parent chain, through snapshot agents and left-out
   ones (`hidden`), reaches `root`; the root's own workspace is the session
   row's (`rootWorkspace`), kept even when no agent is left in it. Each
   workspace once, the root's first. */
export function sessionWork(workspaces: WorkspaceDTO[], root: string, rootWorkspace: string | null, hidden: Hidden = {}): { agents: AgentDTO[]; workspaces: WorkspaceDTO[] } {
  const all = entries(workspaces);
  const parentOf = new Map(all.map(({ agent }) => [agent.id, agent.parent_id ?? null]));
  const reaches = (id: string): boolean => {
    const seen = new Set<string>();
    for (let at: string | null = id; at !== null && !seen.has(at); at = parentOf.has(at) ? parentOf.get(at)! : (hidden[at] ?? null)) {
      if (at === root) return true;
      seen.add(at);
    }
    return false;
  };
  const agents: AgentDTO[] = [];
  const out = new Map<string, WorkspaceDTO>();
  const own = rootWorkspace === null ? undefined : workspaces.find((ws) => ws.id === rootWorkspace);
  if (own) out.set(own.id, own);
  for (const { agent, workspace } of all) {
    if (!reaches(agent.id)) continue;
    agents.push(agent);
    if (!out.has(workspace.id)) out.set(workspace.id, workspace);
  }
  return { agents, workspaces: [...out.values()] };
}

/* The workspaces a tree's agents work in, each once, the root's first. */
export function treeWorkspaces(node: AgentNode | null): WorkspaceDTO[] {
  const out = new Map<string, WorkspaceDTO>();
  const walk = (n: AgentNode) => {
    if (!out.has(n.workspace.id)) out.set(n.workspace.id, n.workspace);
    n.children.forEach(walk);
  };
  if (node) walk(node);
  return [...out.values()];
}
