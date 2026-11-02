import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { MapEdge } from "../../api/client";
import { layoutGraph, MapGraph, NODE_H, type GraphNode } from "./MapGraph";

afterEach(cleanup);

function graph(n: number): { nodes: GraphNode[]; edges: MapEdge[] } {
  const nodes: GraphNode[] = Array.from({ length: n }, (_, i) => i % 6 === 0
    ? { id: `p${i}`, kind: "path", path: `src/brindle/area_${i}/module_with_a_long_name.py` }
    : { id: `d${i}`, kind: "doc", title: `Design note ${i} on caching and its many trade-offs` });
  const paths = nodes.filter((node) => node.kind === "path");
  const edges = nodes.filter((node) => node.kind === "doc").map((node, i) => ({ source: node.id, target: paths[i % paths.length].id, kind: i % 9 ? "shared_path" : "supersedes" }));
  return { nodes, edges };
}

it.each([1, 150])("lays out %i nodes the same way every time, inside its bounds, with no pills overlapping", (n) => {
  const { nodes, edges } = graph(n);
  const layout = layoutGraph(nodes, edges);
  expect(layoutGraph(nodes, edges)).toEqual(layout);
  for (const [i, a] of layout.nodes.entries()) {
    expect(a.x - a.w / 2).toBeGreaterThanOrEqual(-0.001);
    expect(a.x + a.w / 2).toBeLessThanOrEqual(layout.width + 0.001);
    expect(a.y - NODE_H / 2).toBeGreaterThanOrEqual(-0.001);
    expect(a.y + NODE_H / 2).toBeLessThanOrEqual(layout.height + 0.001);
    for (const b of layout.nodes.slice(i + 1)) {
      const apart = Math.abs(a.x - b.x) >= (a.w + b.w) / 2 || Math.abs(a.y - b.y) >= NODE_H;
      expect(apart, `${a.node.id} overlaps ${b.node.id}`).toBe(true);
    }
  }
});

it("highlights a hovered node's neighbourhood and zooms, then fits again", () => {
  const nodes: GraphNode[] = [{ id: "p", kind: "path", path: "src/cache" }, { id: "d", kind: "doc", title: "Cache design" }, { id: "x", kind: "doc", title: "Unrelated" }];
  const { container } = render(<MapGraph nodes={nodes} edges={[{ source: "d", target: "p", kind: "shared_path" }]} onOpen={() => {}} />);
  const node = (id: string) => container.querySelector<HTMLElement>(`[data-node="${id}"]`)!;
  fireEvent.mouseEnter(node("d"));
  expect([node("d").style.opacity, node("p").style.opacity, node("x").style.opacity]).toEqual(["1", "1", "0.3"]);
  expect(container.querySelector('[data-source="d"]')!.getAttribute("class")).toBe("text-branch");
  fireEvent.mouseLeave(node("d"));
  expect(node("x").style.opacity).toBe("1");
  const stage = screen.getByTestId("graph-stage"), fitted = stage.style.transform;
  fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
  expect(stage.style.transform).not.toBe(fitted);
  fireEvent.click(screen.getByRole("button", { name: "Fit graph" }));
  expect(stage.style.transform).toBe(fitted);
});
