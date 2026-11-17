/* The routing-goal triangle's geometry: a weight mix (cost, speed, first try)
   is a point in barycentric terms, with Cheaper bottom-left, Faster
   bottom-right and First try at the top (design/routing-goals, option A). */
import type { RoutingWeights } from "../../api/client";

export const W = 420;
export const H = 372;
const PAD = 40;
export const TOP: [number, number] = [W / 2, PAD + 6];
export const BL: [number, number] = [PAD, H - PAD];
export const BR: [number, number] = [W - PAD, H - PAD];

/* What "Reset to Balanced" sets when the engine names none. */
export const BALANCED: RoutingWeights = { cost: 0.4, speed: 0.3, first_try: 0.3 };

/* One arrow-key press moves the point this far (a twentieth of a side). */
export const STEP = (BR[0] - BL[0]) / 20;

export function toPoint(w: RoutingWeights): [number, number] {
  return [w.cost * BL[0] + w.speed * BR[0] + w.first_try * TOP[0], w.cost * BL[1] + w.speed * BR[1] + w.first_try * TOP[1]];
}

/* The weights at (x, y), pulled into the triangle and rounded to whole
   percents that sum to 100. */
export function fromPoint(x: number, y: number): RoutingWeights {
  const det = (BR[1] - TOP[1]) * (BL[0] - TOP[0]) + (TOP[0] - BR[0]) * (BL[1] - TOP[1]);
  const c = ((BR[1] - TOP[1]) * (x - TOP[0]) + (TOP[0] - BR[0]) * (y - TOP[1])) / det;
  const s = ((TOP[1] - BL[1]) * (x - TOP[0]) + (BL[0] - TOP[0]) * (y - TOP[1])) / det;
  return normalise({ cost: c, speed: s, first_try: 1 - c - s });
}

export function normalise(w: RoutingWeights): RoutingWeights {
  const c = Math.max(0, w.cost);
  const s = Math.max(0, w.speed);
  const f = Math.max(0, w.first_try);
  const sum = c + s + f || 1;
  const C = Math.round((c / sum) * 100);
  const S = Math.min(100 - C, Math.round((s / sum) * 100));
  return { cost: C / 100, speed: S / 100, first_try: (100 - C - S) / 100 };
}

const ARROWS: Record<string, [number, number]> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };

/* The weights after an arrow key, or null for any other key. */
export function stepBy(w: RoutingWeights, key: string): RoutingWeights | null {
  const d = ARROWS[key];
  if (!d) return null;
  const [x, y] = toPoint(w);
  return fromPoint(x + d[0] * STEP, y + d[1] * STEP);
}

/* Whole percents, "50,20,30": what a request and a preview are matched by. */
export const weightsKey = (w: RoutingWeights) => [w.cost, w.speed, w.first_try].map((v) => Math.round(v * 100)).join(",");

export const pct = (v: number) => `${Math.round(v * 100)}%`;
