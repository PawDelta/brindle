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
   it sets a crowd's spacing to about SPRING·√(π/GRAVITY)), the horizontal
   stretch that suits wide labels in a wide frame, and the gaps between boxes. */
const SPRING = 80, STEPS = 300, GRAVITY = 1.5, ASPECT = 1.8, GAP_X = 8, GAP_Y = 6, STEP_SCALE = 0.25;

export interface Sim {
  nodes: GraphNode[];
  labels: { full: string; label: string }[];
  /* Dot radius and box width per node. */
  r: Float64Array;
  w: Float64Array;
  /* Positions: u is x / ASPECT, so the forces see a round crowd. */
  u: Float64Array;
  y: Float64Array;
  links: [number, number][];
  pull: Float64Array;
  pinned: Uint8Array;
  dx: Float64Array;
  dy: Float64Array;
}

export interface Point { x: number; y: number; r: number; w: number }

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
export function createSim(nodes: GraphNode[], edges: MapEdge[]): Sim {
  const n = nodes.length;
  const labels = nodes.map((node) => {
    const full = fullTitle(node);
    return { full, label: shorten(full, node.kind === "path" && !node.title) };
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
  const degree = new Float64Array(n);
  for (const [a, b] of links) { degree[a]++; degree[b]++; }
  const r = degree.map((d) => Math.min(13, 4 + 2.2 * Math.sqrt(d)));
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
  return { nodes, labels, r, w, u, y, links, pull, pinned: new Uint8Array(n), dx: new Float64Array(n), dy: new Float64Array(n) };
}

/* Push apart overlapping boxes, by `strength` of each overlap; a pinned
   node stays put and the other takes the whole push. True if any overlapped. */
function separate(s: Sim, strength: number): boolean {
  const { u, y, r, w, pinned } = s, n = u.length;
  let any = false;
  for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
    const ox = (w[i] + w[j]) / 2 + GAP_X - Math.abs(u[i] - u[j]) * ASPECT;
    if (ox <= 0) continue;
    const oy = r[i] + r[j] + LABEL_SPACE + GAP_Y - Math.abs(y[i] - y[j]);
    if (oy <= 0) continue;
    any = true;
    if (pinned[i] && pinned[j]) continue;
    const mine = pinned[j] ? 1 : pinned[i] ? 0 : 0.5;
    if (ox < oy) {
      const sign = u[i] < u[j] || (u[i] === u[j] && i < j) ? -1 : 1, d = (ox * strength + 0.5) / ASPECT;
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
  for (const [a, b] of s.links) {
    const ex = u[a] - u[b], ey = y[a] - y[b], f = Math.hypot(ex, ey) / SPRING;
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
  return { x: s.u[i] * ASPECT, y: s.y[i], r: s.r[i], w: s.w[i] };
}

export function moveTo(s: Sim, i: number, x: number, y: number): void {
  s.u[i] = x / ASPECT;
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
