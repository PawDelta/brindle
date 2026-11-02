import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { MapEdge } from "../../api/client";
import { boxOf, createSim, layoutGraph } from "./graphLayout";
import { MapGraph, type GraphNode } from "./MapGraph";

afterEach(cleanup);

function graph(n: number): { nodes: GraphNode[]; edges: MapEdge[] } {
  const nodes: GraphNode[] = Array.from({ length: n }, (_, i) => i % 6 === 0
    ? { id: `p${i}`, kind: "path", path: `src/brindle/area_${i}/module_with_a_long_name.py` }
    : { id: `d${i}`, kind: "doc", title: `Design note ${i} on caching and its many trade-offs` });
  const paths = nodes.filter((node) => node.kind === "path");
  const edges = nodes.filter((node) => node.kind === "doc").map((node, i) => ({ source: node.id, target: paths[i % paths.length].id, kind: i % 9 ? "shared_path" : "supersedes" }));
  return { nodes, edges };
}

it.each([1, 150])("lays out %i nodes the same way every time, with no node's dot and label overlapping another's", (n) => {
  const { nodes, edges } = graph(n);
  const points = layoutGraph(nodes, edges);
  expect(layoutGraph(nodes, edges)).toEqual(points);
  const boxes = points.map(boxOf);
  for (const [i, a] of boxes.entries()) for (const b of boxes.slice(i + 1)) {
    const apart = a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top;
    expect(apart, `${JSON.stringify(a)} overlaps ${JSON.stringify(b)}`).toBe(true);
  }
});

it("finds the same groups every time, one per cluster, named after the cluster's best-connected code area", () => {
  const star = (hub: string, docs: number): { nodes: GraphNode[]; edges: MapEdge[] } => {
    const leaves: GraphNode[] = Array.from({ length: docs }, (_, i) => ({ id: `${hub}-d${i}`, kind: "doc", title: `${hub} note ${i}` }));
    return { nodes: [{ id: hub, kind: "path", path: `src/${hub}` }, ...leaves], edges: leaves.map((leaf) => ({ source: leaf.id, target: hub, kind: "shared_path" })) };
  };
  const a = star("cache", 5), b = star("daemon", 3);
  const nodes = [...b.nodes, ...a.nodes, { id: "lone", kind: "doc", title: "Lone" } as GraphNode], edges = [...a.edges, ...b.edges];
  const sim = createSim(nodes, edges);
  expect(createSim(nodes, edges).group).toEqual(sim.group);
  const groupOf = (prefix: string) => new Set(nodes.flatMap((node, i) => (node.id.startsWith(prefix) ? [sim.group[i]] : [])));
  const [cache, daemon] = [groupOf("cache"), groupOf("daemon")];
  expect([cache.size, daemon.size]).toEqual([1, 1]);
  expect(sim.groups.map(({ name, size, tone }) => [name, size, tone])).toEqual([["src/cache", 6, 0], ["src/daemon", 4, 1], ["Lone", 1, null]]);
});

it("highlights a hovered node's neighbourhood, selects on click, moves a dragged node without opening it, and zooms and fits", () => {
  const nodes: GraphNode[] = [{ id: "p", kind: "path", path: "src/cache" }, { id: "d", kind: "doc", title: "Cache design" }, { id: "x", kind: "doc", title: "Unrelated" }];
  const onOpen = vi.fn();
  const { container } = render(<MapGraph nodes={nodes} edges={[{ source: "d", target: "p", kind: "shared_path" }]} onOpen={onOpen} />);
  const node = (id: string) => container.querySelector<HTMLElement>(`[data-node="${id}"]`)!;
  fireEvent.mouseEnter(node("d"));
  expect([node("d").style.opacity, node("p").style.opacity, node("x").style.opacity]).toEqual(["1", "1", "0.2"]);
  expect(container.querySelector('[data-source="d"]')!.getAttribute("class")).toBe("text-branch");
  fireEvent.mouseLeave(node("d"));
  expect(node("x").style.opacity).toBe("1");
  fireEvent.click(screen.getByRole("checkbox", { name: /src\/cache/ }));
  expect([node("d").style.opacity, node("p").style.opacity, node("x").style.opacity]).toEqual(["1", "1", "0.2"]);
  fireEvent.click(screen.getByRole("checkbox", { name: /src\/cache/ }));
  expect(node("x").style.opacity).toBe("1");

  fireEvent.click(node("d"));
  expect(onOpen).toHaveBeenCalledWith(nodes[1]);
  expect(screen.getByRole("complementary", { name: "Node details" }).textContent).toContain("src/cache");
  fireEvent.click(screen.getByRole("button", { name: "Close details" }));
  expect(screen.queryByRole("complementary")).toBeNull();

  const left = parseFloat(node("x").style.left);
  fireEvent.pointerDown(node("x"), { pointerId: 1, clientX: 10, clientY: 10 });
  fireEvent.pointerMove(node("x"), { pointerId: 1, clientX: 70, clientY: 10 });
  fireEvent.pointerUp(node("x"), { pointerId: 1, clientX: 70, clientY: 10 });
  fireEvent.click(node("x"), { detail: 1 });
  expect(parseFloat(node("x").style.left)).toBeGreaterThan(left + 20);
  expect(onOpen).toHaveBeenCalledTimes(1);

  const stage = screen.getByTestId("graph-stage"), fitted = stage.style.transform;
  fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
  expect(stage.style.transform).not.toBe(fitted);
  fireEvent.click(screen.getByRole("button", { name: "Fit graph" }));
  expect(stage.style.transform).toBe(fitted);
});
