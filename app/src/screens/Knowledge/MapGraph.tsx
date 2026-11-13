import type { DecisionDTO } from "../../api/schema";
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from "react";
import type { DocScope, KbSource, KnowledgeMap, MapEdge, MapNode } from "../../api/client";
import { bounds, createSim, docGraph, heatAt, moveTo, pointOf, settle, step, type DocEdge, type Group, type Point, type Sim } from "./graphLayout";

export type LocalNode = ({ id: string; kind: "decision"; title: string; decision: DecisionDTO } | { id: string; kind: "imported"; title: string; source: KbSource; path: string }) & { paths?: string[]; topics?: string[] };
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

export function joinLocal(
  map: KnowledgeMap,
  decisions: DecisionDTO[],
  sources: KbSource[],
  repo: string,
  filter: { scope: DocScope; paths: string[] },
): { nodes: GraphNode[]; edges: MapEdge[] } {
  const nodes: GraphNode[] = [...map.nodes];
  const edges = [...map.edges];
  // Local records have no team or organization membership.
  if (filter.scope.kind !== "user") return { nodes, edges };
  const whole = filter.paths.length === 0;
  // The local library exists independently of hosted documents, including
  // unscoped decisions and imported folders outside the repo checkout.
  if (whole) {
    const local: LocalNode[] = decisions.map((decision) => ({
      id: `local:decision:${decision.id}`, kind: "decision", title: decision.topic,
      decision, paths: decision.scope, topics: [decision.topic],
    }));
    for (const source of sources) {
      if (source.kind !== "folder" || !source.scope) continue;
      local.push({ id: `local:source:${source.id}`, kind: "imported",
        title: source.scope.replace(/\/+$/, "").split("/").at(-1) || source.scope,
        source, path: source.scope, paths: [source.scope] });
    }
    const held = (node: GraphNode): string[] => node.kind === "decision" ? node.decision.scope
      : node.kind === "imported" ? [node.path] : node.paths ?? (node.path ? [node.path] : []);
    const normal = (path: string) => relative(path, repo) ?? path;
    for (let i = 0; i < local.length; i++) {
      for (const other of [...map.nodes, ...local.slice(i + 1)]) {
        const shared = held(local[i]).some((a) => held(other).some((b) => {
          const left = normal(a), right = normal(b);
          if (left.startsWith("/") !== right.startsWith("/")) return false;
          return decisionMatches(left, right)
            || (local[i].kind === "imported" && containsPath(left, right))
            || (other.kind === "imported" && containsPath(right, left))
            || (local[i].kind === "decision" && other.kind === "imported" && decisionMatches(left, `${right}/**`));
        }));
        if (shared) edges.push({ source: local[i].id, target: other.id, kind: "shared_path" });
      }
    }
    return { nodes: [...nodes, ...local], edges };
  }
  const selected = filter.paths.map((p) => relative(p, repo)).filter((p): p is string => p !== null);
  const nodePaths = (node: MapNode) => [...(node.paths ?? []), ...(node.path ? [node.path] : [])]
    .map((p) => relative(p, repo))
    .filter((p): p is string => p !== null);
  // An empty filter is the whole scope: every concrete path in the map is its own area.
  const areas = selected.length ? selected : map.nodes.flatMap(nodePaths);
  const touchesArea = (path: string, area: string) => containsPath(area, path) || decisionMatches(area, path) || decisionMatches(`${area}/**`, path);
  // A document in the neighbourhood may mention paths outside the chosen area.
  // Only paths inside that area may connect a local record to the document.
  const filteredPaths = (node: MapNode, scoped = areas) => nodePaths(node)
    .filter((p) => scoped.some((area) => touchesArea(p, area)));
  const local: { node: LocalNode; paths: string[] }[] = [
    ...decisions.map((d) => ({ node: { id: `local:decision:${d.id}`, kind: "decision" as const, title: d.topic, decision: d }, paths: d.scope })),
  ];
  for (const { node, paths } of local) {
    const scopes = paths.map((p) => relative(p, repo)).filter((p): p is string => p !== null);
    // A concrete path inside the area is a witness even for wildcard decisions.
    // A broad server glob still needs the local scope to intersect that area.
    const matches = map.nodes.filter((n) => areas.some((area) => scopes.some((scope) =>
      filteredPaths(n, [area]).some((path) => {
        const concrete = !/[*?\[]/.test(path);
        return (concrete || touchesArea(scope, area)) && decisionMatches(scope, path);
      }),
    )));
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
      const matches = filteredPaths(server).some((path) => containsPath(scope, path));
      if (!matches) continue;
      if (!joined) { nodes.push(node); joined = true; }
      edges.push({ source: node.id, target: server.id, kind: "shared_path" });
    }
  }
  return { nodes, edges };
}

/* Where a node came from. With `unrecorded` false (the details panel), a
   missing field is left out instead of named. */
function provenance(node: GraphNode, unrecorded = true): string[] {
  const missing = (text: string) => (unrecorded ? text : "");
  const line = (...parts: (string | false | undefined)[]) => parts.filter(Boolean).join(" · ");
  const date = (seconds?: number) => {
    const value = seconds === undefined ? null : new Date(seconds * 1000);
    return value && Number.isFinite(value.getTime()) ? value.toISOString().slice(0, 10) : missing("Date unrecorded");
  };
  const scope = (kind?: string) => kind === "user" || kind === "personal" ? "Personal" : kind === "group" || kind === "team" ? "Team" : kind === "org" ? "Org" : kind || missing("Scope unrecorded");
  if (node.kind === "path") return [];
  if (node.kind === "decision") return [node.decision.author_session ? `Session ${node.decision.author_session}` : missing("Session/author unrecorded"), line("Local decision", date(node.decision.created_at))].filter(Boolean);
  if (node.kind === "imported") return [node.path, line("Imported on this device", scope(node.source.visibility), date(node.source.added_at))];
  if (node.local_path) return [node.local_path, line("Indexed on this device", date(node.updated_at))];
  const source = node.source;
  return [
    line(source?.ref, source?.session && `Session ${source.session}`) || missing("Session/branch unrecorded"),
    node.created_by || missing("Author/agent unrecorded"),
    line(`${scope(node.scope?.kind)}${node.scope && node.scope.kind !== "user" ? ` ${node.scope.name || node.scope.id}` : ""}`, date(node.created_at)),
  ].filter(Boolean);
}

/* Colour says which group a node belongs to; shape says what it is. */
const KINDS = {
  path: { label: "Code area", diamond: false, hollow: true },
  doc: { label: "Document", diamond: false, hollow: false },
  decision: { label: "Local decision", diamond: true, hollow: false },
  imported: { label: "Imported folder", diamond: true, hollow: true },
} as const;

const toneOf = (group: Group | undefined) => group?.tone == null ? "var(--ink-3)" : `var(--group-${group.tone + 1})`;

/* A node's mark, `size` across: a ring or a dot, a hollow or a filled diamond. */
function Mark({ kind, size, colour }: { kind: GraphNode["kind"]; size: number; colour: string }) {
  const { diamond, hollow } = KINDS[kind], side = diamond ? size * 0.74 : size;
  const fill = hollow ? { border: `${Math.max(1.5, size * 0.16)}px solid ${colour}`, background: "var(--canvas-2)" } : { background: colour };
  return <span aria-hidden className="grid shrink-0 place-items-center" style={{ width: size, height: size }}>
    <span className={diamond ? "rotate-45 rounded-[2px]" : "rounded-full"} style={{ width: side, height: side, ...fill }} />
  </span>;
}

/* A gentle curve between dot centres, always bent the same way; an arrow
   stops at the rim of the target's dot. */
function curve(a: Point, b: Point, arrow: boolean): string {
  const ex = b.x - a.x, ey = b.y - a.y, len = Math.hypot(ex, ey) || 1, bend = Math.min(28, len * 0.12);
  const cx = (a.x + b.x) / 2 - (ey / len) * bend, cy = (a.y + b.y) / 2 + (ex / len) * bend;
  let tx = b.x, ty = b.y;
  if (arrow) {
    const ux = b.x - cx, uy = b.y - cy, ul = Math.hypot(ux, uy) || 1;
    tx -= (ux / ul) * (b.r + 2); ty -= (uy / ul) * (b.r + 2);
  }
  return `M${a.x.toFixed(1)} ${a.y.toFixed(1)}Q${cx.toFixed(1)} ${cy.toFixed(1)} ${tx.toFixed(1)} ${ty.toFixed(1)}`;
}

type View = { k: number; x: number; y: number };
type Gesture = { node: number | null; start: { x: number; y: number }; grab: { x: number; y: number }; moved: boolean };
const MAX_ZOOM = 4, MIN_ZOOM = 0.2, FIT_PAD = 0.04, FIT_MAX = 2, LEGEND_GROUPS = 8, PANEL_W = 256, LABEL_ZOOM = 0.65;
/* The frame fills a parent that has a height, and otherwise (the Knowledge
   page scrolls) takes most of the window. */
const FRAME_H = "max(380px, 70vh)";
const control = "grid h-7 w-7 place-items-center rounded-md border border-line-strong bg-canvas-2 text-sm leading-none text-ink-2 hover:bg-surface-2 hover:text-ink focus-visible:outline-2 focus-visible:outline-branch";
const halo = { textShadow: "0 0 3px var(--canvas-2), 0 0 2px var(--canvas-2), 0 0 1px var(--canvas-2)" };

/* Fit with about 4% padding on each side, above the legend. */
function fitView(s: Sim, w: number, h: number): View {
  const b = bounds(s), bw = Math.max(b.right - b.left, 1), bh = Math.max(b.bottom - b.top, 1);
  const padX = w * FIT_PAD, padY = h * FIT_PAD, room = h - 2 * padY;
  const k = Math.min(FIT_MAX, (w - 2 * padX) / bw, room / bh);
  return { k, x: (w - bw * k) / 2 - b.left * k, y: padY + (room - bh * k) / 2 - b.top * k };
}

/* The map as documents and how they connect: code paths are not nodes but
   what links documents, and a document's paths are listed in its details,
   where clicking one narrows the map to it. */
export function MapGraph({ nodes: given, edges: givenEdges, onOpen, onDeselect, preview, onDoc }: { nodes: GraphNode[]; edges: MapEdge[]; onOpen(node: GraphNode): void; onDeselect?(): void; preview?: { node: string; summary: string } | null; onDoc?(): void }) {
  const { nodes, edges, paths } = useMemo(() => docGraph(given, givenEdges), [given, givenEdges]);
  const frame = useRef<HTMLDivElement>(null);
  /* The frame's width, and its height above the legend. */
  const [size, setSize] = useState({ w: 800, h: 420, frameH: 480 });
  const legend = useRef<HTMLDivElement>(null);
  /* The picture is drawn about as wide as the frame, in steps of a quarter
     so that small resizes keep it; a different shape lays it out again. */
  /* From the whole frame, so the legend re-wrapping (when the panel opens)
     doesn't lay the graph out again. */
  const shape = Math.round(Math.min(3.5, Math.max(1, size.w / Math.max(size.frameH - 56, 1))) * 4) / 4;
  /* The settled start is the same for the same graph and shape; dragging
     moves the nodes in this object from then on, outside React's render. */
  const sim = useMemo(() => settle(createSim(nodes, edges, shape)), [nodes, edges, shape]);
  const index = useMemo(() => new Map(nodes.map((node, i) => [node.id, i])), [nodes]);
  const lines = useMemo(() => edges.flatMap((edge) => {
    const a = index.get(edge.source), b = index.get(edge.target);
    return a === undefined || b === undefined || a === b ? [] : [{ edge, a, b }];
  }), [edges, index]);
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
  /* Until the person pans, zooms or drags, the view follows the fit; a new
     layout drops their view, and a new graph their selection. */
  const [manual, setManual] = useState<{ sim: Sim; view: View } | null>(null);
  const [picked, setPicked] = useState<{ nodes: GraphNode[]; id: string } | null>(null);
  /* The node under the pointer and the node with keyboard focus, apart, so
     the pointer passing over the graph doesn't drop a focused node's light. */
  const [hover, setHover] = useState<string | null>(null);
  const [focused, setFocused] = useState<string | null>(null);
  /* Whether the legend lists every group or just the largest. */
  const [allGroups, setAllGroups] = useState(false);
  /* A group picked in the legend, for the same edges (groups come from them). */
  const [shown, setShown] = useState<{ lines: typeof lines; group: number } | null>(null);
  const fitted = fitView(sim, size.w, size.h);
  const view = manual?.sim === sim ? manual.view : fitted;
  const selected = picked?.nodes === nodes ? picked.id : null;
  const focusGroup = shown?.lines === lines ? shown.group : null;
  // Closing the details, Escape or a background click all end the selection; the owner drops what it had started for it.
  const hadSelection = useRef(false);
  const deselected = useRef(onDeselect);
  deselected.current = onDeselect;
  useEffect(() => {
    if (selected === null && hadSelection.current) deselected.current?.();
    hadSelection.current = selected !== null;
  }, [selected]);
  const live = useRef({ view, sim, fitted, size, lines, selected });
  live.current = { view, sim, fitted, size, lines, selected };
  const uid = `kg${useId().replace(/[^\w-]/g, "")}`;
  const nodeEls = useRef<(HTMLButtonElement | null)[]>([]);
  const edgeEls = useRef<(SVGPathElement | null)[]>([]);

  useLayoutEffect(() => {
    const el = frame.current;
    if (!el) return;
    const measure = () => {
      const below = legend.current ? legend.current.offsetHeight + 8 : 0;
      if (el.clientWidth && el.clientHeight) setSize({ w: el.clientWidth, h: Math.max(160, el.clientHeight - below), frameH: el.clientHeight });
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    if (legend.current) observer.observe(legend.current);
    return () => observer.disconnect();
  }, []);

  /* Moves nodes and edges in the DOM straight from the simulation. */
  const paint = useCallback(() => {
    const { sim: s, lines: ls } = live.current;
    nodeEls.current.forEach((el, i) => {
      if (!el || i >= s.nodes.length) return;
      const p = pointOf(s, i);
      el.style.left = `${p.x - p.w / 2}px`;
      el.style.top = `${p.y - p.r}px`;
    });
    ls.forEach(({ edge, a, b }, j) => edgeEls.current[j]?.setAttribute("d", curve(pointOf(s, a), pointOf(s, b), edge.kind === "supersedes")));
  }, []);

  /* The live simulation: reheated by a drag, held warm while it lasts, then
     cooling until it stops. */
  const motion = useRef({ alpha: 0, frame: 0, dragging: false });
  const run = useCallback(() => {
    const m = motion.current;
    if (m.frame || typeof requestAnimationFrame !== "function") return;
    const tick = () => {
      step(live.current.sim, heatAt(m.alpha), true);
      paint();
      m.alpha = m.dragging ? Math.max(m.alpha * 0.98, 0.3) : m.alpha * 0.985;
      m.frame = m.alpha > 0.02 ? requestAnimationFrame(tick) : 0;
    };
    m.frame = requestAnimationFrame(tick);
  }, [paint]);
  useEffect(() => () => {
    const m = motion.current;
    if (m.frame) cancelAnimationFrame(m.frame);
    m.frame = 0;
  }, []);

  const apply = useCallback((next: (v: View) => View) => {
    const v = next(live.current.view);
    live.current.view = v;
    setManual({ sim: live.current.sim, view: v });
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
      // The details panel and the legend scroll their own lists.
      if ((e.target as Element).closest("aside, [data-legend]")) return;
      e.preventDefault();
      const r = el.getBoundingClientRect(), delta = e.deltaY * (e.deltaMode === 1 ? 16 : 1);
      zoomAt(Math.exp(-delta * (e.ctrlKey ? 0.01 : 0.0015)), e.clientX - r.left, e.clientY - r.top);
    };
    el.addEventListener("wheel", wheel, { passive: false });
    return () => el.removeEventListener("wheel", wheel);
  }, [zoomAt]);

  /* A node or the person's selection, brought into view (left of the panel). */
  const reveal = useCallback((i: number, panel: boolean) => {
    const { view: v, size: s, sim: sm } = live.current, p = pointOf(sm, i), m = 12;
    const left = (p.x - p.w / 2) * v.k + v.x, right = (p.x + p.w / 2) * v.k + v.x;
    const top = (p.y - p.r) * v.k + v.y, bottom = (p.y + p.r + 16) * v.k + v.y, edge = s.w - (panel ? PANEL_W : 0) - m;
    const shiftX = left < m ? m - left : right > edge ? edge - right : 0;
    const shiftY = top < m ? m - top : bottom > s.h ? s.h - bottom : 0;
    if (shiftX || shiftY) apply((c) => ({ ...c, x: c.x + shiftX, y: c.y + shiftY }));
  }, [apply]);

  /* Drag a node to move it (the rest settle around it, and it stays where
     it is dropped); drag the background to pan, or pinch with two pointers. */
  const gesture = useRef<Gesture | null>(null);
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const swallow = useRef(false);
  const toGraph = (clientX: number, clientY: number) => {
    const r = frame.current!.getBoundingClientRect(), v = live.current.view;
    return { x: (clientX - r.left - v.x) / v.k, y: (clientY - r.top - v.y) / v.k };
  };
  const down = (e: ReactPointerEvent<HTMLDivElement>) => {
    swallow.current = false;
    const target = e.target as Element, start = { x: e.clientX, y: e.clientY };
    const node = target.closest<HTMLElement>("[data-node]");
    if (node) {
      const i = index.get(node.dataset.node!);
      gesture.current = i === undefined ? null : { node: i, start, grab: { x: 0, y: 0 }, moved: false };
      return;
    }
    if (target.closest("button, aside")) return;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    pointers.current.set(e.pointerId, start);
    gesture.current = { node: null, start, grab: { x: 0, y: 0 }, moved: false };
  };
  const move = (e: ReactPointerEvent<HTMLDivElement>) => {
    const g = gesture.current;
    if (g?.node != null) {
      const s = live.current.sim;
      if (!g.moved) {
        if (Math.hypot(e.clientX - g.start.x, e.clientY - g.start.y) < 4) return;
        g.moved = true;
        e.currentTarget.setPointerCapture?.(e.pointerId);
        const p = toGraph(g.start.x, g.start.y), at = pointOf(s, g.node);
        g.grab = { x: at.x - p.x, y: at.y - p.y };
        s.pinned[g.node] = 1;
        setManual({ sim: s, view: live.current.view });
        motion.current.dragging = true;
        motion.current.alpha = Math.max(motion.current.alpha, 0.3);
        run();
      }
      const p = toGraph(e.clientX, e.clientY);
      moveTo(s, g.node, p.x + g.grab.x, p.y + g.grab.y);
      paint();
      return;
    }
    const map = pointers.current, prev = map.get(e.pointerId);
    if (!prev || !g) return;
    const next = { x: e.clientX, y: e.clientY };
    if (Math.hypot(next.x - g.start.x, next.y - g.start.y) > 3) g.moved = true;
    const other = [...map].find(([id]) => id !== e.pointerId)?.[1];
    map.set(e.pointerId, next);
    if (!other) { apply((v) => ({ ...v, x: v.x + next.x - prev.x, y: v.y + next.y - prev.y })); return; }
    const r = e.currentTarget.getBoundingClientRect();
    const before = Math.hypot(prev.x - other.x, prev.y - other.y) || 1, after = Math.hypot(next.x - other.x, next.y - other.y);
    apply((v) => ({ ...v, x: v.x + (next.x - prev.x) / 2, y: v.y + (next.y - prev.y) / 2 }));
    zoomAt(after / before, (next.x + other.x) / 2 - r.left, (next.y + other.y) / 2 - r.top);
  };
  const up = (e: ReactPointerEvent<HTMLDivElement>) => {
    const g = gesture.current;
    pointers.current.delete(e.pointerId);
    if (g?.node != null) {
      if (g.moved) { swallow.current = true; motion.current.dragging = false; }
      gesture.current = null;
      return;
    }
    if (g && !g.moved && !pointers.current.size) setPicked(null);
    if (!pointers.current.size) gesture.current = null;
  };
  const pick = (e: ReactMouseEvent, i: number) => {
    // A drag that ends over its node is not a click; a key press always is.
    if (swallow.current && e.detail !== 0) { swallow.current = false; return; }
    setPicked({ nodes, id: nodes[i].id });
    // Hold the view still while the panel opens and the legend re-wraps.
    setManual({ sim, view: live.current.view });
    reveal(i, true);
    onOpen(nodes[i]);
  };

  /* What is lit: the hovered node's neighbourhood, else the group picked in
     the legend, else the selected node's neighbourhood. */
  const active = hover ?? focused ?? (focusGroup === null ? selected : null);
  const inGroup = (i: number) => focusGroup !== null && sim.group[i] === focusGroup;
  const lit = active ? near.get(active) ?? new Set<string>() : focusGroup !== null ? new Set(nodes.filter((_, i) => inGroup(i)).map((node) => node.id)) : null;
  const chosen = selected === null ? undefined : index.get(selected);
  const kinds = new Set(nodes.map((node) => node.kind));
  const named = sim.groups.flatMap((group, g) => (group.tone === null ? [] : [{ group, g }]));
  const alone = sim.groups.length - named.length;
  return (
    <div
      ref={frame}
      role="group"
      aria-label="Library graph"
      className="relative cursor-grab touch-none select-none overflow-hidden rounded-lg border border-line bg-canvas-2 active:cursor-grabbing"
      style={{ height: "100%", minHeight: FRAME_H }}
      onPointerDown={down}
      onPointerMove={move}
      onPointerUp={up}
      onPointerCancel={up}
      onKeyDown={(e) => { if (e.key === "Escape" && !e.defaultPrevented && selected !== null) { e.preventDefault(); setPicked(null); } }}
      onScroll={(e) => { e.currentTarget.scrollTop = 0; e.currentTarget.scrollLeft = 0; }}
    >
      <div data-testid="graph-stage" className="absolute left-0 top-0 origin-top-left" style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})` }}>
        <svg className="pointer-events-none absolute left-0 top-0 overflow-visible" width="1" height="1" aria-label="Connections">
          <defs>
            {[["", "text-ink-3"], ["-on", "text-branch"]].map(([suffix, tone]) => (
              <marker key={suffix} id={`${uid}${suffix}`} className={tone} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" markerUnits="userSpaceOnUse" orient="auto-start-reverse">
                <path d="M0 1L10 5L0 9z" fill="currentColor" />
              </marker>
            ))}
          </defs>
          {lines.map(({ edge, a, b }, j) => {
            const supersedes = edge.kind === "supersedes";
            const on = active !== null ? edge.source === active || edge.target === active : inGroup(a) && inGroup(b);
            return <g key={j} data-source={edge.source} data-target={edge.target} data-kind={edge.kind} className={on ? "text-branch" : "text-ink-3"} style={{ opacity: on ? 0.9 : lit ? 0.06 : supersedes ? 0.55 : 0.35, transition: "opacity 150ms" }}>
              <path ref={(el) => { edgeEls.current[j] = el; }} d={curve(pointOf(sim, a), pointOf(sim, b), supersedes)} fill="none" stroke="currentColor" strokeWidth={Math.min(4, 1 + 0.75 * ((edge as DocEdge).weight ?? 1) - 0.75) + (on ? 0.5 : 0)} vectorEffect="non-scaling-stroke" strokeDasharray={supersedes ? "4 3" : undefined} markerEnd={supersedes ? `url(#${uid}${on ? "-on" : ""})` : undefined} />
              <title>{(edge as DocEdge).shared ? `Shares ${(edge as DocEdge).shared!.join(", ")}` : edge.kind.replace(/_/g, " ")}</title>
            </g>;
          })}
        </svg>
        {nodes.map((node, i) => {
          const kind = KINDS[node.kind], p = pointOf(sim, i), { full, label } = sim.labels[i], about = provenance(node);
          const dim = lit !== null && node.id !== active && !lit.has(node.id), on = node.id === active;
          /* Zoomed out, labels give way to the dots, except around the node in focus. */
          const labelled = view.k >= LABEL_ZOOM || on || node.id === selected || (lit !== null && lit.has(node.id));
          return <button
            key={node.id}
            ref={(el) => { nodeEls.current[i] = el; }}
            type="button"
            data-node={node.id}
            aria-label={`${kind.label}: ${full}`}
            aria-describedby={about.length ? `${uid}-n${i}` : undefined}
            title={[full, ...about].join("\n")}
            onClick={(e) => pick(e, i)}
            onMouseEnter={() => setHover(node.id)}
            onMouseLeave={() => setHover((h) => (h === node.id ? null : h))}
            onFocus={() => { setFocused(node.id); reveal(i, chosen !== undefined); }}
            onBlur={() => setFocused((f) => (f === node.id ? null : f))}
            style={{ left: p.x - p.w / 2, top: p.y - p.r, width: p.w, opacity: dim ? 0.2 : 1 }}
            className={`group absolute flex cursor-pointer flex-col items-center outline-none transition-opacity duration-150 ${on ? "z-10" : ""}`}
          >
            <span className={`shrink-0 rounded-full ring-branch ring-offset-2 ring-offset-canvas-2 group-hover:ring-2 group-focus-visible:ring-2 ${node.id === selected ? "ring-2" : ""}`}>
              <Mark kind={node.kind} size={2 * p.r} colour={toneOf(sim.groups[sim.group[i]])} />
            </span>
            <span className={`mt-[3px] text-[11px] leading-[13px] whitespace-nowrap ${on ? "text-ink" : "max-w-full truncate text-ink-2"} ${labelled ? "" : "invisible"}`} style={halo}>{on ? full : label}</span>
            {about.length > 0 && <span id={`${uid}-n${i}`} className="sr-only">{about.join(" · ")}</span>}
          </button>;
        })}
      </div>
      <div className="absolute left-2 top-2 flex flex-col gap-1">
        <button type="button" aria-label="Zoom in" title="Zoom in" className={control} onClick={() => zoomAt(1.25, size.w / 2, size.h / 2)}>+</button>
        <button type="button" aria-label="Zoom out" title="Zoom out" className={control} onClick={() => zoomAt(0.8, size.w / 2, size.h / 2)}>−</button>
        <button type="button" aria-label="Fit graph" title="Fit graph" className={control} onClick={() => setManual(null)}>
          <svg aria-hidden width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M1 5V1h4M9 1h4v4M13 9v4H9M5 13H1V9" /></svg>
        </button>
      </div>
      <div ref={legend} className="pointer-events-none absolute bottom-2 left-2 flex flex-col gap-1 rounded-md bg-canvas-2/90 px-2 py-1 text-[11px] text-ink-3" style={{ maxWidth: chosen === undefined ? "calc(100% - 16px)" : `max(160px, calc(100% - ${PANEL_W + 16}px))` }}>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          {(Object.keys(KINDS) as (keyof typeof KINDS)[]).filter((k) => kinds.has(k)).map((k) => (
            <span key={k} className="inline-flex items-center gap-1.5"><Mark kind={k} size={9} colour="var(--ink-3)" />{KINDS[k].label}</span>
          ))}
          {lines.some(({ edge }) => edge.kind === "supersedes") && <span className="inline-flex items-center gap-1.5">
            <svg aria-hidden width="22" height="8" viewBox="0 0 22 8"><path d="M1 4H16" stroke="currentColor" strokeDasharray="3 2.5" /><path d="M15 1L21 4L15 7z" fill="currentColor" /></svg>Supersedes
          </span>}
          <span>Size: connections</span>
        </div>
        {named.length > 0 && <div data-legend className={`flex flex-wrap items-center gap-x-1 gap-y-0.5 ${allGroups ? "pointer-events-auto max-h-28 overflow-y-auto" : ""}`}>
          {(allGroups ? named : named.slice(0, LEGEND_GROUPS)).map(({ group, g }) => (
            <button key={g} type="button" role="checkbox" aria-checked={focusGroup === g} title={`${group.name} · ${group.size}`} onClick={() => setShown(focusGroup === g ? null : { lines, group: g })} className="pointer-events-auto inline-flex max-w-56 items-center gap-1.5 rounded-sm px-1 py-px hover:bg-surface-2 hover:text-ink aria-checked:bg-surface-2 aria-checked:text-ink">
              <span aria-hidden className="h-2 w-2 shrink-0 rounded-full" style={{ background: toneOf(group) }} />
              <span className="truncate">{group.name}</span><span className="shrink-0">· {group.size}</span>
            </button>
          ))}
          {named.length > LEGEND_GROUPS && <button type="button" aria-expanded={allGroups} onClick={() => setAllGroups(!allGroups)} className="pointer-events-auto rounded-sm px-1 py-px underline decoration-dotted underline-offset-2 hover:bg-surface-2 hover:text-ink">
            {allGroups ? "Fewer groups" : `+${named.length - LEGEND_GROUPS} more`}
          </button>}
          {alone > 0 && <span className="px-1">Unconnected · {alone}</span>}
        </div>}
      </div>
      {chosen !== undefined && <Details node={nodes[chosen]} full={sim.labels[chosen].full} group={sim.groups[sim.group[chosen]]} summary={preview?.node === nodes[chosen].id ? preview.summary : ""} onDoc={selected === nodes[chosen].id ? onDoc : undefined} paths={paths.get(nodes[chosen].id) ?? []} onPath={(path) => onOpen({ id: `path:${path}`, kind: "path", path })} neighbours={[...(near.get(nodes[chosen].id) ?? [])].flatMap((id) => {
        const i = index.get(id);
        return i === undefined ? [] : [{ node: nodes[i], full: sim.labels[i].full, group: sim.groups[sim.group[i]] }];
      })} onClose={() => setPicked(null)} />}
    </div>
  );
}

/* The selected node's details, beside the graph like an inspector. */
function Details({ node, full, group, summary, onDoc, paths, onPath, neighbours, onClose }: { node: GraphNode; full: string; group: Group; summary: string; onDoc?(): void; paths: string[]; onPath(path: string): void; neighbours: { node: GraphNode; full: string; group: Group }[]; onClose(): void }) {
  const kind = KINDS[node.kind];
  return <aside aria-label="Node details" className="absolute inset-y-0 right-0 flex max-w-[85%] cursor-auto select-text flex-col gap-2 overflow-auto border-l border-line bg-surface p-3 text-xs" style={{ width: PANEL_W }}>
    <div className="flex items-center justify-between gap-2">
      <span className="inline-flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-ink-3"><Mark kind={node.kind} size={10} colour={toneOf(group)} />{kind.label}</span>
      <button type="button" aria-label="Close details" title="Close details" className="rounded-md px-1.5 text-sm leading-none text-ink-3 hover:bg-surface-2 hover:text-ink" onClick={onClose}>×</button>
    </div>
    <p className="break-words text-sm font-medium text-ink">{full}</p>
    {provenance(node, false).map((line, i) => <p key={i} data-provenance className="break-words text-ink-3">{line}</p>)}
    {summary && <p data-summary className="break-words text-ink-2">{summary}</p>}
    {onDoc && <button type="button" className="self-start rounded-md border border-line px-2 py-1 text-ink hover:bg-surface-2" onClick={onDoc}>Open document</button>}
    <p className="flex min-w-0 items-center gap-1.5 text-ink-3">
      <span aria-hidden className="h-2 w-2 shrink-0 rounded-full" style={{ background: toneOf(group) }} />
      <span className="truncate">{group.tone === null ? "Unconnected" : `Group: ${group.name} · ${group.size}`}</span>
    </p>
    {paths.length > 0 && <div className="flex flex-col gap-0.5">
      <p className="text-ink-3">Code paths</p>
      {paths.map((path) => <button key={path} type="button" aria-label={`Code area: ${path}`} title={`Narrow the map to ${path}`} onClick={() => onPath(path)} className="truncate rounded-sm px-1 text-left font-mono text-[11px] text-ink-2 hover:bg-surface-2 hover:text-ink">{path}</button>)}
    </div>}
    <p className="text-ink-3">{neighbours.length} {neighbours.length === 1 ? "connection" : "connections"}</p>
    {neighbours.length > 0 && <ul className="flex flex-col gap-1">
      {neighbours.map(({ node: n, full: title, group: g }) => <li key={n.id} className="flex min-w-0 items-center gap-1.5 text-ink-2" title={title}>
        <Mark kind={n.kind} size={8} colour={toneOf(g)} /><span className="truncate">{title}</span>
      </li>)}
    </ul>}
  </aside>;
}
