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
  if (p.startsWith("/") || p.split("/").includes("..")) return null;
  return p.replace(/^\.\//, "").replace(/\/+$/, "");
}

function covers(scope: string, path: string): boolean {
  const pattern = scope.split("/").map((part) => part === "**" ? ".*" : part.split("*").map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("[^/]*")).join("/");
  return !scope || scope === path || path.startsWith(`${scope}/`) || new RegExp(`^${pattern}$`).test(path);
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
      return path !== null && scopes.some((scope) => covers(scope, path));
    }));
    if (!matches.length) continue;
    nodes.push(node);
    for (const match of matches) edges.push({ source: node.id, target: match.id, kind: "shared_path" });
  }
  /* Only paths already present in this neighbourhood are considered. A source
     contributes its matching files without enumerating or uploading the folder. */
  for (const source of sources) {
    if (source.kind !== "folder" || !source.scope) continue;
    const scope = relative(source.scope, repo);
    if (scope === null) continue;
    const imported = new Map<string, LocalNode>();
    for (const server of map.nodes) {
      const paths = new Set([...(server.paths ?? []), ...(server.path ? [server.path] : [])]);
      for (const raw of paths) {
        const path = relative(raw, repo);
        if (path === null || !covers(scope, path)) continue;
        let node = imported.get(path);
        if (!node) {
          node = { id: `local:source:${source.id}:${path}`, kind: "imported", title: path.split("/").at(-1) || path, source, path: `${repo.replace(/\/+$/, "")}/${path}` };
          imported.set(path, node);
          nodes.push(node);
        }
        edges.push({ source: node.id, target: server.id, kind: "shared_path" });
      }
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
