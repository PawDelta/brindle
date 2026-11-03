/* The knowledge map's force simulation, with no randomness: the same graph
   always settles into the same picture, and the same forces keep running
   live while a node is dragged, so the rest settle around it. */
import type { MapEdge } from "../../api/client";
import type { GraphNode } from "./MapGraph";

/* A node is a dot sized by its connections with its label underneath. The
   layout keeps each node's whole box, dot and label, clear of the others. */
export const LABEL_SPACE = 16;
const LABEL_CHARS = 26, CHAR_W = 6.2, LABEL_PAD = 6;
/* Ideal edge length, settling steps, pull to the centre (with 1/d repulsion
   it sets a crowd's spacing to about SPRING·√(π/GRAVITY)), and the gaps
   between boxes. */
const SPRING = 80, STEPS = 300, GRAVITY = 1.5, GAP_X = 8, GAP_Y = 6, STEP_SCALE = 0.25;

export interface Sim {
  nodes: GraphNode[];
  labels: { full: string; label: string }[];
  /* Dot radius and box width per node. */
  r: Float64Array;
  w: Float64Array;
  /* How much wider than tall the picture is drawn, to suit the frame; the
     forces work on u = x / aspect, so they see a round crowd. */
  aspect: number;
  u: Float64Array;
  y: Float64Array;
  /* One link per connected pair, with its weight (how much the two share)
     and what they share. */
  links: [number, number][];
  weights: Float64Array;
  shared: string[][];
  degree: Float64Array;
  /* Each node's group (an index into groups), found from the edges. */
  group: Int32Array;
  groups: Group[];
  pull: Float64Array;
  pinned: Uint8Array;
  dx: Float64Array;
  dy: Float64Array;
}

export interface Point { x: number; y: number; r: number; w: number }

/* A group of connected knowledge, named after what its members most often
   share. Its tone is a palette slot (0-9), or null for a node on its own. */
export interface Group { name: string; size: number; tone: number | null }
export const TONES = 10;

/* A link between two documents (or a local record and a document): an
   explicit relation, or the paths and topics the two share. */
export interface DocEdge extends MapEdge { weight?: number; shared?: string[] }
export interface DocGraph { nodes: GraphNode[]; edges: DocEdge[]; paths: Map<string, string[]> }

const pathOf = (node: GraphNode) => (node.kind === "path" ? node.path || node.title || node.id : null);
const topicsOf = (node: GraphNode) => {
  const topics = (node as { topics?: unknown }).topics;
  return Array.isArray(topics) ? topics.filter((t): t is string => typeof t === "string" && t.trim() !== "") : [];
};

/* The map as knowledge records. Code paths stop being nodes: two records
   that share paths or topics get one link, weighted by how many they share,
   and explicit relations stay. Local records are knowledge in their own
   right, even when the hosted library is empty. */
export function docGraph(nodes: GraphNode[], edges: MapEdge[]): DocGraph {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const docs = nodes.filter((node) => node.kind !== "path");
  const paths = new Map(docs.map((doc) => [doc.id, new Set(doc.paths ?? [])]));
  for (const edge of edges) {
    const a = byId.get(edge.source), b = byId.get(edge.target);
    if (!a || !b) continue;
    for (const [one, other] of [[a, b], [b, a]]) {
      const path = pathOf(other);
      if (path === null) continue;
      if (one.kind !== "path") paths.get(one.id)!.add(path);
    }
  }
  const holders = new Map<string, string[]>();
  for (const doc of docs) {
    for (const key of [...paths.get(doc.id)!, ...topicsOf(doc)]) {
      if (!holders.has(key)) holders.set(key, []);
      const list = holders.get(key)!;
      if (list.at(-1) !== doc.id) list.push(doc.id);
    }
  }
  const pairs = new Map<string, DocEdge>();
  for (const [key, list] of holders) {
    if (list.length < 2) continue;
    for (let i = 0; i < list.length; i++) for (let j = i + 1; j < list.length; j++) {
      const id = `${list[i]}\0${list[j]}`, pair = pairs.get(id);
      if (pair) { pair.weight = (pair.weight ?? 0) + 1; pair.shared?.push(key); }
      else pairs.set(id, { source: list[i], target: list[j], kind: "shares", weight: 1, shared: [key] });
    }
  }
  const out: DocEdge[] = [...pairs.values()];
  for (const edge of edges) {
    const a = byId.get(edge.source), b = byId.get(edge.target);
    if (!a || !b || a.kind === "path" || b.kind === "path") continue;
    out.push(edge);
  }
  return {
    nodes: docs,
    edges: out,
    paths: new Map([...paths].map(([id, held]) => [id, [...held].sort()])),
  };
}

export function fullTitle(node: GraphNode): string {
  return node.title || (node.kind === "path" ? node.path : undefined) || node.id;
}

/* A long path keeps its tail (the file), a long title its head. */
function shorten(text: string, tail: boolean): string {
  if (text.length <= LABEL_CHARS) return text;
  return tail ? `…${text.slice(-(LABEL_CHARS - 1))}` : `${text.slice(0, LABEL_CHARS - 1).trimEnd()}…`;
}

const RANK = { path: 0, doc: 1, decision: 2, imported: 3 } as const;

/* Nodes start on a golden-angle spiral in (kind, id) order. */
export function createSim(nodes: GraphNode[], edges: MapEdge[], aspect = 1.8): Sim {
  const n = nodes.length;
  const labels = nodes.map((node) => {
    const full = fullTitle(node);
    return { full, label: shorten(full, node.kind === "path" && !node.title) };
  });
  const index = new Map(nodes.map((node, i) => [node.id, i]));
  const linked = new Map<string, { a: number; b: number; weight: number; shared: string[] }>();
  for (const edge of edges as DocEdge[]) {
    const a = index.get(edge.source), b = index.get(edge.target);
    if (a === undefined || b === undefined || a === b) continue;
    const key = a < b ? `${a} ${b}` : `${b} ${a}`, held = linked.get(key);
    if (held) { held.weight += edge.weight ?? 1; held.shared.push(...(edge.shared ?? [])); }
    else linked.set(key, { a, b, weight: edge.weight ?? 1, shared: [...(edge.shared ?? [])] });
  }
  const links = [...linked.values()].map(({ a, b }): [number, number] => [a, b]);
  const weights = Float64Array.from(linked.values(), ({ weight }) => weight);
  const shared = [...linked.values()].map((link) => link.shared);
  const degree = new Float64Array(n);
  for (const [a, b] of links) { degree[a]++; degree[b]++; }
  /* Radius grows with connections on a square-root scale: 5 at one, 16 for
     the best-connected node in the map. */
  const top = Math.max(1, ...degree) - 1;
  const r = degree.map((d) => (d ? 5 + 11 * (top ? Math.sqrt((d - 1) / top) : 0) : 4.5));
  const w = Float64Array.from(labels, ({ label }, i) => Math.max(2 * r[i], Math.ceil(label.length * CHAR_W + LABEL_PAD)));
  /* A node with no edges would drift to the rim of the crowd and shrink the
     rest of the picture when it is fitted; it is pulled in harder. */
  const pull = degree.map((d) => (d ? GRAVITY : GRAVITY * 4));
  const u = new Float64Array(n), y = new Float64Array(n);
  const order = nodes.map((_, i) => i).sort((a, b) => RANK[nodes[a].kind] - RANK[nodes[b].kind] || (nodes[a].id < nodes[b].id ? -1 : nodes[a].id > nodes[b].id ? 1 : a - b));
  order.forEach((i, rank) => {
    const angle = rank * 2.399963229728653, d = SPRING * 0.6 * Math.sqrt(rank + 0.5);
    u[i] = d * Math.cos(angle);
    y[i] = d * Math.sin(angle);
  });
  const categories = nodes.map((node) => (node.kind === "doc" ? node.category : undefined));
  const { group, groups } = communities(categories, links, weights, shared, degree, order, labels);
  return { nodes, labels, r, w, aspect, u, y, links, weights, shared, degree, group, groups, pull, pinned: new Uint8Array(n), dx: new Float64Array(n), dy: new Float64Array(n) };
}

/* A doc's group is its category, when the server gave it one. Without one
   it falls back to its community: Louvain's local-moving phase over the
   weighted links, with no randomness (nodes are visited in (kind, id) order
   and each joins the neighbouring group that gains the most modularity,
   staying put on a tie, else taking the lowest group). Groups are numbered
   largest first, ties to the one visited first; a community is named after
   the path or topic its members share most, else its best-connected member,
   and marked uncategorised when the map has categories at all. */
function communities(categories: (string | undefined)[], links: [number, number][], weights: Float64Array, shared: string[][], degree: Float64Array, order: number[], labels: { full: string }[]) {
  const n = categories.length;
  const strength = new Float64Array(n), adj: [number, number][][] = Array.from({ length: n }, () => []);
  links.forEach(([a, b], l) => { strength[a] += weights[l]; strength[b] += weights[l]; adj[a].push([b, weights[l]]); adj[b].push([a, weights[l]]); });
  const m2 = strength.reduce((sum, k) => sum + k, 0);
  const raw = Int32Array.from({ length: n }, (_, i) => i), tot = Float64Array.from(strength);
  for (let pass = 0; pass < 50 && m2; pass++) {
    let moved = false;
    for (const i of order) {
      const k = strength[i], own = raw[i];
      if (!k) continue;
      tot[own] -= k;
      const into = new Map<number, number>();
      for (const [j, wt] of adj[i]) into.set(raw[j], (into.get(raw[j]) ?? 0) + wt);
      let best = own, gain = (into.get(own) ?? 0) - (tot[own] * k) / m2;
      for (const c of [...into.keys()].sort((a, b) => a - b)) {
        const g = into.get(c)! - (tot[c] * k) / m2;
        if (g > gain + 1e-9) { best = c; gain = g; }
      }
      tot[best] += k;
      if (best !== own) { raw[i] = best; moved = true; }
    }
    if (!moved) break;
  }
  const rank = new Int32Array(n);
  order.forEach((i, at) => { rank[i] = at; });
  const members = new Map<string, number[]>();
  for (const i of order) {
    const key = categories[i] ? `c\0${categories[i]}` : `m\0${raw[i]}`;
    if (!members.has(key)) members.set(key, []);
    members.get(key)!.push(i);
  }
  const categorised = categories.some(Boolean);
  const sorted = [...members.values()].sort((a, b) => b.length - a.length || rank[a[0]] - rank[b[0]]);
  const group = new Int32Array(n);
  sorted.forEach((list, g) => { for (const i of list) group[i] = g; });
  const counts = sorted.map(() => new Map<string, number>());
  links.forEach(([a, b], l) => {
    if (group[a] !== group[b]) return;
    for (const key of shared[l]) counts[group[a]].set(key, (counts[group[a]].get(key) ?? 0) + 1);
  });
  let tone = 0;
  const groups = sorted.map((list, g) => {
    const category = categories[list[0]];
    if (category) return { name: category, size: list.length, tone: tone++ % TONES };
    const top = [...counts[g]].sort(([a, x], [b, y]) => y - x || (a < b ? -1 : a > b ? 1 : 0))[0]?.[0];
    const lead = list.reduce((best, i) => (degree[i] > degree[best] ? i : best), list[0]);
    const name = top ?? labels[lead].full;
    return { name: categorised ? `Uncategorised: ${name}` : name, size: list.length, tone: list.length > 1 ? tone++ % TONES : null };
  });
  return { group, groups };
}

/* Push apart overlapping boxes, by `strength` of each overlap; a pinned
   node stays put and the other takes the whole push. True if any overlapped. */
function separate(s: Sim, strength: number): boolean {
  const { u, y, r, w, pinned, aspect } = s, n = u.length;
  let any = false;
  for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
    const ox = (w[i] + w[j]) / 2 + GAP_X - Math.abs(u[i] - u[j]) * aspect;
    if (ox <= 0) continue;
    const oy = r[i] + r[j] + LABEL_SPACE + GAP_Y - Math.abs(y[i] - y[j]);
    if (oy <= 0) continue;
    any = true;
    if (pinned[i] && pinned[j]) continue;
    const mine = pinned[j] ? 1 : pinned[i] ? 0 : 0.5;
    if (ox < oy) {
      const sign = u[i] < u[j] || (u[i] === u[j] && i < j) ? -1 : 1, d = (ox * strength + 0.5) / aspect;
      u[i] += sign * d * mine; u[j] -= sign * d * (1 - mine);
    } else {
      const sign = y[i] < y[j] || (y[i] === y[j] && i < j) ? -1 : 1, d = oy * strength + 0.5;
      y[i] += sign * d * mine; y[j] -= sign * d * (1 - mine);
    }
  }
  return any;
}

/* One step of Fruchterman-Reingold (1/d repulsion, d² springs, linear pull
   to the centre), each move capped at `heat`; then a soft collision pass. */
export function step(s: Sim, heat: number, collide: boolean): void {
  const { u, y, dx, dy, pinned, pull } = s, n = u.length;
  dx.fill(0); dy.fill(0);
  for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
    let ex = u[i] - u[j], ey = y[i] - y[j], d2 = ex * ex + ey * ey;
    if (d2 < 0.01) { ex = j - i; ey = 1; d2 = ex * ex + 1; }
    const f = (SPRING * SPRING) / d2;
    dx[i] += ex * f; dy[i] += ey * f; dx[j] -= ex * f; dy[j] -= ey * f;
  }
  for (const [l, [a, b]] of s.links.entries()) {
    // Documents that share more pull closer.
    const ex = u[a] - u[b], ey = y[a] - y[b], f = (Math.hypot(ex, ey) / SPRING) * (1 + 0.25 * (s.weights[l] - 1));
    dx[a] -= ex * f; dy[a] -= ey * f; dx[b] += ex * f; dy[b] += ey * f;
  }
  for (let i = 0; i < n; i++) {
    if (pinned[i]) continue;
    dx[i] -= u[i] * pull[i]; dy[i] -= y[i] * pull[i];
    const len = Math.hypot(dx[i], dy[i]) * STEP_SCALE, move = len > heat ? (heat / len) * STEP_SCALE : STEP_SCALE;
    u[i] += dx[i] * move; y[i] += dy[i] * move;
  }
  if (collide) separate(s, 0.5);
}

/* The heat a live step runs at, for a given alpha (1 = fully reheated). */
export const heatAt = (alpha: number) => alpha * SPRING * 0.5;

/* Cool from hot to still, then remove every overlap that is left. */
export function settle(s: Sim): Sim {
  for (let i = 0; i < STEPS; i++) step(s, heatAt(1 - i / STEPS) + 0.5, i > STEPS / 3);
  for (let pass = 0; pass < 1000 && separate(s, 1); pass++);
  return s;
}

export function pointOf(s: Sim, i: number): Point {
  return { x: s.u[i] * s.aspect, y: s.y[i], r: s.r[i], w: s.w[i] };
}

export function moveTo(s: Sim, i: number, x: number, y: number): void {
  s.u[i] = x / s.aspect;
  s.y[i] = y;
}

/* The box a node keeps clear: its label's width (or the dot's, if wider),
   from the top of the dot to the foot of the label. */
export function boxOf(p: Point) {
  return { left: p.x - p.w / 2, right: p.x + p.w / 2, top: p.y - p.r, bottom: p.y + p.r + LABEL_SPACE };
}

export function bounds(s: Sim) {
  let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
  for (let i = 0; i < s.nodes.length; i++) {
    const b = boxOf(pointOf(s, i));
    left = Math.min(left, b.left); right = Math.max(right, b.right);
    top = Math.min(top, b.top); bottom = Math.max(bottom, b.bottom);
  }
  return s.nodes.length ? { left, top, right, bottom } : { left: 0, top: 0, right: 0, bottom: 0 };
}

export function layoutGraph(nodes: GraphNode[], edges: MapEdge[]): Point[] {
  const s = settle(createSim(nodes, edges));
  return nodes.map((_, i) => pointOf(s, i));
}
