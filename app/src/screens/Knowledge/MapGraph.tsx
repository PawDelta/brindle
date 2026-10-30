import type { DecisionDTO } from "../../api/schema";
import type { KbSource, KnowledgeMap, MapEdge, MapNode } from "../../api/client";
import { card } from "../../components/ui";

export type LocalNode = { id: string; kind: "decision"; title: string; decision: DecisionDTO } | { id: string; kind: "imported"; title: string; source: KbSource; path: string };
export type GraphNode = MapNode | LocalNode;

/* Compare path segments, never topic/title similarity or raw string prefixes.
   Absolute folders outside this repo cannot accidentally join a relative path. */
function relative(path: string, repo: string): string | null {
  const root = repo.replace(/\/+$/, "");
  if (path === root) return "";
  const p = path.startsWith(`${root}/`) ? path.slice(root.length + 1) : path;
  if (p.startsWith("/")) return null;
  const parts: string[] = [];
  for (const part of p.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (!parts.length) return null;
      parts.pop();
    } else parts.push(part);
  }
  return parts.join("/");
}

/* Python fnmatch semantics: wildcards also match separators, brackets use
   shell character classes, and a backslash is a literal character. */
function fnmatch(pattern: string, path: string): boolean {
  const chars = Array.from(pattern);
  const tokens: (null | ((char: string) => boolean))[] = [];
  for (let i = 0; i < chars.length; i++) {
    const char = chars[i];
    if (char === "*") { tokens.push(null); continue; }
    if (char === "?") { tokens.push(() => true); continue; }
    if (char === "[") {
      let end = i + 1;
      if (chars[end] === "!") end++;
      if (chars[end] === "]") end++;
      while (end < chars.length && chars[end] !== "]") end++;
      if (end < chars.length) {
        const negated = chars[i + 1] === "!";
        const members = chars.slice(i + (negated ? 2 : 1), end);
        tokens.push((value) => {
          let found = false;
          for (let j = 0; j < members.length; j++) {
            if (j + 2 < members.length && members[j + 1] === "-") {
              found ||= value.codePointAt(0)! >= members[j].codePointAt(0)! && value.codePointAt(0)! <= members[j + 2].codePointAt(0)!;
              j += 2;
            } else found ||= value === members[j];
          }
          return negated ? !found : found;
        });
        i = end;
        continue;
      }
    }
    tokens.push((value) => value === char);
  }
  const text = Array.from(path);
  let previous = new Uint8Array(text.length + 1);
  previous[0] = 1;
  for (const token of tokens) {
    const next = new Uint8Array(text.length + 1);
    if (token === null) next[0] = previous[0];
    for (let j = 1; j <= text.length; j++) {
      next[j] = Number(token === null ? previous[j] || next[j - 1] : previous[j - 1] && token(text[j - 1]));
    }
    previous = next;
  }
  return previous[text.length] === 1;
}

/* tasks._glob_match also tries PurePath.match in both directions when **
   occurs: a relative pattern may match the trailing path segments. */
function decisionMatches(scope: string, path: string): boolean {
  if (scope === path || fnmatch(scope, path) || fnmatch(path, scope)) return true;
  if (!scope.includes("**") && !path.includes("**")) return false;
  const suffix = (pattern: string, value: string) => {
    const parts = pattern.split("/"), segments = value.split("/");
    return parts.length <= segments.length && parts.every((part, i) => fnmatch(part, segments[segments.length - parts.length + i]));
  };
  return suffix(scope, path) || suffix(path, scope);
}

function containsPath(folder: string, path: string): boolean {
  return !folder || folder === path || path.startsWith(`${folder}/`);
}

export function joinLocal(map: KnowledgeMap, decisions: DecisionDTO[], sources: KbSource[], repo: string): { nodes: GraphNode[]; edges: MapEdge[] } {
  const nodes: GraphNode[] = [...map.nodes];
  const edges = [...map.edges];
  const local: { node: LocalNode; paths: string[] }[] = [
    ...decisions.map((d) => ({ node: { id: `local:decision:${d.id}`, kind: "decision" as const, title: d.topic, decision: d }, paths: d.scope })),
  ];
  for (const { node, paths } of local) {
    const scopes = paths.map((p) => relative(p, repo)).filter((p): p is string => p !== null);
    const matches = map.nodes.filter((n) => [...(n.paths ?? []), ...(n.path ? [n.path] : [])].some((p) => {
      const path = relative(p, repo);
      return path !== null && scopes.some((scope) => decisionMatches(scope, path));
    }));
    if (!matches.length) continue;
    nodes.push(node);
    for (const match of matches) edges.push({ source: node.id, target: match.id, kind: "shared_path" });
  }
  /* KbSource describes a folder, not its file inventory. Represent only that
     actual folder and open its recorded path, even when it has no documents. */
  for (const source of sources) {
    if (source.kind !== "folder" || !source.scope) continue;
    const scope = relative(source.scope, repo);
    if (scope === null) continue;
    const node: LocalNode = { id: `local:source:${source.id}`, kind: "imported", title: source.scope.replace(/\/+$/, "").split("/").at(-1) || source.scope, source, path: source.scope };
    let joined = false;
    for (const server of map.nodes) {
      const paths = new Set([...(server.paths ?? []), ...(server.path ? [server.path] : [])]);
      const matches = [...paths].some((raw) => {
        const path = relative(raw, repo);
        return path !== null && containsPath(scope, path);
      });
      if (!matches) continue;
      if (!joined) { nodes.push(node); joined = true; }
      edges.push({ source: node.id, target: server.id, kind: "shared_path" });
    }
  }
  return { nodes, edges };
}

export function MapGraph({ nodes, edges, onOpen }: { nodes: GraphNode[]; edges: MapEdge[]; onOpen(node: GraphNode): void }) {
  const columns = [nodes.filter((n) => n.kind === "path"), nodes.filter((n) => n.kind === "doc"), nodes.filter((n) => n.kind === "decision" || n.kind === "imported")];
  const points = new Map(columns.flatMap((col, x) => col.map((n, y) => [n.id, { x: x * 300 + 140, y: y * 100 + 56 }] as const)));
  const height = Math.max(320, ...columns.map((c) => c.length * 100 + 24));
  return (
    <div className="overflow-auto rounded-lg border border-line bg-surface-2" aria-label="Knowledge graph">
      <div className="relative min-w-[900px]" style={{ height }}>
        <svg className="pointer-events-none absolute inset-0 h-full w-full" aria-label="Connections">
          {edges.map((edge, i) => {
            const a = points.get(edge.source), b = points.get(edge.target);
            return a && b ? <g key={i} data-source={edge.source} data-target={edge.target} data-kind={edge.kind}>
              <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="currentColor" className="text-ink-3" strokeOpacity="0.35" strokeDasharray={edge.kind === "supersedes" ? "5 4" : undefined} />
              <title>{edge.kind}</title>
            </g> : null;
          })}
        </svg>
        {nodes.map((node) => {
          const p = points.get(node.id)!;
          return <button key={node.id} type="button" data-node={node.id} onClick={() => onOpen(node)} style={{ left: p.x - 120, top: p.y - 32 }} className={`${card} absolute flex h-16 w-60 flex-col justify-center gap-1 px-s1 text-left hover:border-branch focus-visible:outline-branch`}>
            <span className={`text-xs ${node.kind === "decision" ? "text-fawn" : node.kind === "doc" ? "text-branch" : "text-ink-3"}`}>{node.kind === "path" ? "Code area" : node.kind === "imported" ? "Imported · local" : node.kind === "decision" ? "Decision · local" : "Document"}</span>
            <span className="w-full truncate text-sm font-medium">{node.title || (node.kind === "path" ? node.path : node.id)}</span>
          </button>;
        })}
      </div>
    </div>
  );
}
