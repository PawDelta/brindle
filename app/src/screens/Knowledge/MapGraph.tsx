import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { DecisionDTO } from "../../api/schema";
import type { KbSource, KnowledgeMap, MapEdge, MapNode } from "../../api/client";

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

/* Each kind is a coloured mark (shape as well as colour, so the kinds stay
   apart without colour) at the front of a pill holding the title. */
const KINDS = {
  path: { label: "Code area", rank: 0, mark: "h-2.5 w-2.5 rounded-[2px] bg-ink-3" },
  doc: { label: "Document", rank: 1, mark: "h-2.5 w-2.5 rounded-full bg-branch" },
  decision: { label: "Local decision", rank: 2, mark: "mx-px h-2 w-2 rotate-45 rounded-[1px] bg-fawn" },
  imported: { label: "Imported folder", rank: 3, mark: "h-2.5 w-2.5 rounded-full border-2 border-ink-2" },
} as const;

/* Pill geometry, in graph units: the layout sizes each pill from its label,
   and the label is clipped to that size, so pills it keeps apart stay apart. */
export const NODE_H = 22;
const LABEL_CHARS = 28, CHAR_W = 7, PILL_PAD = 40;
/* Force layout: ideal edge length, steps, pull to the centre (with 1/d
   repulsion, it sets the spacing of a crowd to about SPRING·√(π/GRAVITY)),
   and the horizontal stretch that suits wide pills in a wide frame. */
const SPRING = 80, STEPS = 300, GRAVITY = 1.5, ASPECT = 2.2, GAP_X = 10, GAP_Y = 8;

export interface Placed { node: GraphNode; x: number; y: number; w: number; label: string; full: string }
export interface Layout { nodes: Placed[]; width: number; height: number }

function fullTitle(node: GraphNode): string {
  return node.title || (node.kind === "path" ? node.path : undefined) || node.id;
}

/* A long path keeps its tail (the file), a long title its head. */
function shorten(text: string, tail: boolean): string {
  if (text.length <= LABEL_CHARS) return text;
  return tail ? `…${text.slice(-(LABEL_CHARS - 1))}` : `${text.slice(0, LABEL_CHARS - 1).trimEnd()}…`;
}

/* Deterministic: nodes start on a golden-angle spiral in (kind, id) order,
   a Fruchterman-Reingold simulation with no randomness settles them, and a
   final pass pushes apart any pills that still overlap. Positions are pill
   centres, shifted so the whole graph starts at (0, 0). */
export function layoutGraph(nodes: GraphNode[], edges: MapEdge[]): Layout {
  const n = nodes.length;
  const labels = nodes.map((node) => {
    const full = fullTitle(node);
    return { full, label: shorten(full, node.kind === "path" && !node.title) };
  });
  const w = labels.map(({ label }) => Math.ceil(PILL_PAD + label.length * CHAR_W));
  const x = new Float64Array(n), y = new Float64Array(n);
  const order = nodes.map((_, i) => i).sort((a, b) => KINDS[nodes[a].kind].rank - KINDS[nodes[b].kind].rank || (nodes[a].id < nodes[b].id ? -1 : nodes[a].id > nodes[b].id ? 1 : a - b));
  order.forEach((i, r) => {
    const angle = r * 2.399963229728653, d = SPRING * 0.6 * Math.sqrt(r + 0.5);
    x[i] = d * Math.cos(angle);
    y[i] = d * Math.sin(angle);
  });
  const index = new Map(nodes.map((node, i) => [node.id, i]));
  const links: [number, number][] = [];
  const linked = new Set<string>();
  for (const edge of edges) {
    const a = index.get(edge.source), b = index.get(edge.target);
    if (a === undefined || b === undefined || a === b) continue;
    const key = a < b ? `${a} ${b}` : `${b} ${a}`;
    if (!linked.has(key)) { linked.add(key); links.push([a, b]); }
  }
  /* A node with no edges would drift to the rim of the crowd and shrink the
     rest of the picture when it is fitted; it is pulled in harder. */
  const pull = new Float64Array(n).fill(GRAVITY * 4);
  for (const [a, b] of links) pull[a] = pull[b] = GRAVITY;
  const dx = new Float64Array(n), dy = new Float64Array(n);
  for (let step = 0; step < STEPS; step++) {
    const heat = SPRING * 0.5 * (1 - step / STEPS) + 0.5;
    dx.fill(0); dy.fill(0);
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      let ex = x[i] - x[j], ey = y[i] - y[j], d2 = ex * ex + ey * ey;
      if (d2 < 0.01) { ex = j - i; ey = 1; d2 = ex * ex + 1; }
      const f = (SPRING * SPRING) / d2;
      dx[i] += ex * f; dy[i] += ey * f; dx[j] -= ex * f; dy[j] -= ey * f;
    }
    for (const [a, b] of links) {
      const ex = x[a] - x[b], ey = y[a] - y[b], f = Math.hypot(ex, ey) / SPRING;
      dx[a] -= ex * f; dy[a] -= ey * f; dx[b] += ex * f; dy[b] += ey * f;
    }
    for (let i = 0; i < n; i++) {
      dx[i] -= x[i] * pull[i]; dy[i] -= y[i] * pull[i];
      const len = Math.hypot(dx[i], dy[i]), move = len > heat ? heat / len : 1;
      x[i] += dx[i] * move; y[i] += dy[i] * move;
    }
  }
  for (let i = 0; i < n; i++) x[i] *= ASPECT;
  for (let pass = 0; pass < 1000; pass++) {
    let moved = false;
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      const ox = (w[i] + w[j]) / 2 + GAP_X - Math.abs(x[i] - x[j]), oy = NODE_H + GAP_Y - Math.abs(y[i] - y[j]);
      if (ox <= 0 || oy <= 0) continue;
      moved = true;
      if (ox < oy) {
        const s = x[i] < x[j] || (x[i] === x[j] && i < j) ? -1 : 1;
        x[i] += (s * ox) / 2 + s * 0.25; x[j] -= (s * ox) / 2 + s * 0.25;
      } else {
        const s = y[i] < y[j] || (y[i] === y[j] && i < j) ? -1 : 1;
        y[i] += (s * oy) / 2 + s * 0.25; y[j] -= (s * oy) / 2 + s * 0.25;
      }
    }
    if (!moved) break;
  }
  let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
  for (let i = 0; i < n; i++) {
    left = Math.min(left, x[i] - w[i] / 2); right = Math.max(right, x[i] + w[i] / 2);
    top = Math.min(top, y[i] - NODE_H / 2); bottom = Math.max(bottom, y[i] + NODE_H / 2);
  }
  if (!n) left = top = right = bottom = 0;
  return {
    nodes: nodes.map((node, i) => ({ node, x: x[i] - left, y: y[i] - top, w: w[i], ...labels[i] })),
    width: right - left,
    height: bottom - top,
  };
}

/* Where the curve from (cx, cy) into a pill's centre crosses its edge, so an
   arrowhead lands on the pill instead of under it. */
function border(p: Placed, cx: number, cy: number, gap: number): [number, number] {
  const vx = cx - p.x, vy = cy - p.y, len = Math.hypot(vx, vy) || 1;
  const s = Math.min(vx ? (p.w / 2) / Math.abs(vx) : Infinity, vy ? (NODE_H / 2) / Math.abs(vy) : Infinity, 1);
  return [p.x + vx * s + (vx / len) * gap, p.y + vy * s + (vy / len) * gap];
}

/* A gentle quadratic curve, always bent the same way, so parallel straight
   lines fan out and the picture stays the same for the same input. */
function curve(a: Placed, b: Placed, arrow: boolean): string {
  const ex = b.x - a.x, ey = b.y - a.y, len = Math.hypot(ex, ey) || 1, bend = Math.min(48, len * 0.18);
  const cx = (a.x + b.x) / 2 - (ey / len) * bend, cy = (a.y + b.y) / 2 + (ex / len) * bend;
  const [tx, ty] = arrow ? border(b, cx, cy, 2) : [b.x, b.y];
  return `M${a.x.toFixed(1)} ${a.y.toFixed(1)}Q${cx.toFixed(1)} ${cy.toFixed(1)} ${tx.toFixed(1)} ${ty.toFixed(1)}`;
}

type View = { k: number; x: number; y: number };
const MAX_ZOOM = 3, MIN_ZOOM = 0.2, FIT_PAD = 24, LEGEND_H = 34;
const control = "grid h-7 w-7 place-items-center rounded-md border border-line-strong bg-canvas-2 text-sm leading-none text-ink-2 hover:bg-surface-2 hover:text-ink focus-visible:outline-2 focus-visible:outline-branch";

function fitView(layout: Layout, w: number, h: number): View {
  const room = h - FIT_PAD - LEGEND_H;
  const k = Math.min(1, (w - 2 * FIT_PAD) / Math.max(layout.width, 1), room / Math.max(layout.height, 1));
  return { k, x: (w - layout.width * k) / 2, y: FIT_PAD + (room - layout.height * k) / 2 };
}

export function MapGraph({ nodes, edges, onOpen }: { nodes: GraphNode[]; edges: MapEdge[]; onOpen(node: GraphNode): void }) {
  const layout = useMemo(() => layoutGraph(nodes, edges), [nodes, edges]);
  const at = useMemo(() => new Map(layout.nodes.map((p) => [p.node.id, p])), [layout]);
  const near = useMemo(() => {
    const m = new Map<string, Set<string>>();
    for (const { source, target } of edges) {
      if (!m.has(source)) m.set(source, new Set());
      if (!m.has(target)) m.set(target, new Set());
      m.get(source)!.add(target);
      m.get(target)!.add(source);
    }
    return m;
  }, [edges]);
  const height = Math.round(Math.min(620, Math.max(300, 220 + Math.sqrt(nodes.length) * 34)));
  const frame = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: height });
  /* Until the person pans or zooms, the view follows the fit; a new graph
     drops their view and fits again. */
  const [manual, setManual] = useState<{ layout: Layout; view: View } | null>(null);
  const fitted = fitView(layout, size.w, size.h);
  const view = manual?.layout === layout ? manual.view : fitted;
  const live = useRef({ view, layout, fitted, size });
  live.current = { view, layout, fitted, size };
  const [active, setActive] = useState<string | null>(null);
  const marker = `kg${useId().replace(/[^\w-]/g, "")}`;
  const kinds = new Set(nodes.map((node) => node.kind));

  useLayoutEffect(() => {
    const el = frame.current;
    if (!el) return;
    const measure = () => { if (el.clientWidth && el.clientHeight) setSize({ w: el.clientWidth, h: el.clientHeight }); };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [height]);

  const apply = useCallback((next: (v: View) => View) => {
    const v = next(live.current.view);
    live.current.view = v;
    setManual({ layout: live.current.layout, view: v });
  }, []);
  const zoomAt = useCallback((factor: number, px: number, py: number) => apply((v) => {
    const k = Math.min(MAX_ZOOM, Math.max(Math.min(MIN_ZOOM, live.current.fitted.k), v.k * factor));
    return { k, x: px - ((px - v.x) * k) / v.k, y: py - ((py - v.y) * k) / v.k };
  }), [apply]);

  /* React's wheel listener is passive; zooming must keep the page still. */
  useEffect(() => {
    const el = frame.current;
    if (!el) return;
    const wheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect(), delta = e.deltaY * (e.deltaMode === 1 ? 16 : 1);
      zoomAt(Math.exp(-delta * (e.ctrlKey ? 0.01 : 0.0015)), e.clientX - r.left, e.clientY - r.top);
    };
    el.addEventListener("wheel", wheel, { passive: false });
    return () => el.removeEventListener("wheel", wheel);
  }, [zoomAt]);

  /* One pointer on the background pans; two pinch. */
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const down = (e: ReactPointerEvent<HTMLDivElement>) => {
    if ((e.target as Element).closest("button")) return;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
  };
  const move = (e: ReactPointerEvent<HTMLDivElement>) => {
    const map = pointers.current, prev = map.get(e.pointerId);
    if (!prev) return;
    const next = { x: e.clientX, y: e.clientY };
    const other = [...map].find(([id]) => id !== e.pointerId)?.[1];
    map.set(e.pointerId, next);
    if (!other) { apply((v) => ({ ...v, x: v.x + next.x - prev.x, y: v.y + next.y - prev.y })); return; }
    const r = e.currentTarget.getBoundingClientRect();
    const before = Math.hypot(prev.x - other.x, prev.y - other.y) || 1, after = Math.hypot(next.x - other.x, next.y - other.y);
    apply((v) => ({ ...v, x: v.x + (next.x - prev.x) / 2, y: v.y + (next.y - prev.y) / 2 }));
    zoomAt(after / before, (next.x + other.x) / 2 - r.left, (next.y + other.y) / 2 - r.top);
  };
  const up = (e: ReactPointerEvent<HTMLDivElement>) => { pointers.current.delete(e.pointerId); };

  /* Tabbing to a node off screen brings it into view. */
  const reveal = (p: Placed) => {
    const { view: v, size: s } = live.current, m = 12;
    const left = (p.x - p.w / 2) * v.k + v.x, right = (p.x + p.w / 2) * v.k + v.x;
    const top = (p.y - NODE_H / 2) * v.k + v.y, bottom = (p.y + NODE_H / 2) * v.k + v.y;
    const shiftX = left < m ? m - left : right > s.w - m ? s.w - m - right : 0;
    const shiftY = top < m ? m - top : bottom > s.h - LEGEND_H ? s.h - LEGEND_H - bottom : 0;
    if (shiftX || shiftY) apply((c) => ({ ...c, x: c.x + shiftX, y: c.y + shiftY }));
  };

  const lit = active ? near.get(active) ?? new Set<string>() : null;
  return (
    <div
      ref={frame}
      role="group"
      aria-label="Knowledge graph"
      className="relative cursor-grab touch-none select-none overflow-hidden rounded-lg border border-line bg-canvas-2 active:cursor-grabbing"
      style={{ height }}
      onPointerDown={down}
      onPointerMove={move}
      onPointerUp={up}
      onPointerCancel={up}
      onScroll={(e) => { e.currentTarget.scrollTop = 0; e.currentTarget.scrollLeft = 0; }}
    >
      <div data-testid="graph-stage" className="absolute left-0 top-0 origin-top-left" style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})` }}>
        <svg className="pointer-events-none absolute left-0 top-0 overflow-visible" width={Math.max(layout.width, 1)} height={Math.max(layout.height, 1)} aria-label="Connections">
          <defs>
            {[["", "text-ink-3"], ["-on", "text-branch"]].map(([suffix, tone]) => (
              <marker key={suffix} id={`${marker}${suffix}`} className={tone} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto-start-reverse">
                <path d="M0 1L10 5L0 9z" fill="currentColor" />
              </marker>
            ))}
          </defs>
          {edges.map((edge, i) => {
            const a = at.get(edge.source), b = at.get(edge.target);
            if (!a || !b || a === b) return null;
            const supersedes = edge.kind === "supersedes";
            const on = active !== null && (edge.source === active || edge.target === active);
            return <g key={i} data-source={edge.source} data-target={edge.target} data-kind={edge.kind} className={on ? "text-branch" : "text-ink-3"} style={{ opacity: on ? 0.95 : active ? 0.08 : supersedes ? 0.6 : 0.4, transition: "opacity 150ms" }}>
              <path d={curve(a, b, supersedes)} fill="none" stroke="currentColor" strokeWidth={on ? 1.75 : 1.25} vectorEffect="non-scaling-stroke" strokeDasharray={supersedes ? "5 4" : undefined} markerEnd={supersedes ? `url(#${marker}${on ? "-on" : ""})` : undefined} />
              <title>{supersedes ? "supersedes" : edge.kind.replace(/_/g, " ")}</title>
            </g>;
          })}
        </svg>
        {layout.nodes.map((p) => {
          const kind = KINDS[p.node.kind];
          const dim = lit !== null && p.node.id !== active && !lit.has(p.node.id);
          return <button
            key={p.node.id}
            type="button"
            data-node={p.node.id}
            aria-label={`${kind.label}: ${p.full}`}
            title={p.full}
            onClick={() => onOpen(p.node)}
            onMouseEnter={() => setActive(p.node.id)}
            onMouseLeave={() => setActive((a) => (a === p.node.id ? null : a))}
            onFocus={() => { setActive(p.node.id); reveal(p); }}
            onBlur={() => setActive((a) => (a === p.node.id ? null : a))}
            style={{ left: p.x - p.w / 2, top: p.y - NODE_H / 2, width: p.w, height: NODE_H, opacity: dim ? 0.3 : 1 }}
            className={`absolute flex cursor-pointer items-center gap-1.5 rounded-full border bg-surface-2 pl-2 pr-2.5 text-left text-xs leading-none whitespace-nowrap text-ink transition-[opacity,border-color] duration-150 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-branch ${p.node.id === active ? "border-branch" : "border-line-strong"}`}
          >
            <span aria-hidden className={`shrink-0 ${kind.mark}`} />
            <span className="min-w-0 truncate">{p.label}</span>
          </button>;
        })}
      </div>
      <div className="absolute right-2 top-2 flex flex-col gap-1">
        <button type="button" aria-label="Zoom in" title="Zoom in" className={control} onClick={() => zoomAt(1.25, size.w / 2, size.h / 2)}>+</button>
        <button type="button" aria-label="Zoom out" title="Zoom out" className={control} onClick={() => zoomAt(0.8, size.w / 2, size.h / 2)}>−</button>
        <button type="button" aria-label="Fit graph" title="Fit graph" className={control} onClick={() => setManual(null)}>
          <svg aria-hidden width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M1 5V1h4M9 1h4v4M13 9v4H9M5 13H1V9" /></svg>
        </button>
      </div>
      <div className="pointer-events-none absolute bottom-2 left-2 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md bg-canvas-2/90 px-2 py-1 text-[11px] text-ink-3">
        {(Object.keys(KINDS) as (keyof typeof KINDS)[]).filter((k) => kinds.has(k)).map((k) => (
          <span key={k} className="inline-flex items-center gap-1.5"><span aria-hidden className={KINDS[k].mark} />{KINDS[k].label}</span>
        ))}
        {edges.some((e) => e.kind === "supersedes") && <span className="inline-flex items-center gap-1.5">
          <svg aria-hidden width="22" height="8" viewBox="0 0 22 8" className="text-ink-3"><path d="M1 4H16" stroke="currentColor" strokeDasharray="3 2.5" /><path d="M15 1L21 4L15 7z" fill="currentColor" /></svg>Supersedes
        </span>}
      </div>
    </div>
  );
}
