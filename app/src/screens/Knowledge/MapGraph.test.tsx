import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { MapEdge } from "../../api/client";
import { boxOf, createSim, docGraph, layoutGraph } from "./graphLayout";
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

it("links documents that share paths or topics, weighted by how much, and finds the same named groups every time", () => {
  const star = (hub: string, docs: number): { nodes: GraphNode[]; edges: MapEdge[] } => {
    const leaves = Array.from({ length: docs }, (_, i) => ({ id: `${hub}-d${i}`, kind: "doc", title: `${hub} note ${i}`, topics: hub === "cache" && i < 2 ? ["eviction"] : [] }) as GraphNode);
    return { nodes: [{ id: hub, kind: "path", path: `src/${hub}` }, ...leaves], edges: leaves.map((leaf) => ({ source: leaf.id, target: hub, kind: "mentions" })) };
  };
  const a = star("cache", 5), b = star("daemon", 3);
  const g = docGraph([...b.nodes, ...a.nodes, { id: "lone", kind: "doc", title: "Lone" }], [...a.edges, ...b.edges]);
  expect(g.nodes.filter((node) => node.kind === "path")).toEqual([]);
  expect(g.edges.find((e) => e.source === "cache-d0" && e.target === "cache-d1")).toMatchObject({ weight: 2, shared: ["src/cache", "eviction"] });
  // However many documents share a path, they stay linked as one group.
  const wide = star("wide", 13), common = docGraph(wide.nodes, wide.edges);
  expect(common.edges).toHaveLength(78);
  expect(createSim(common.nodes, common.edges).groups.map(({ size }) => size)).toEqual([13]);
  const nodes = g.nodes, sim = createSim(nodes, g.edges);
  expect(createSim(nodes, g.edges).group).toEqual(sim.group);
  const groupOf = (prefix: string) => new Set(nodes.flatMap((node, i) => (node.id.startsWith(prefix) ? [sim.group[i]] : [])));
  const [cache, daemon] = [groupOf("cache"), groupOf("daemon")];
  expect([cache.size, daemon.size]).toEqual([1, 1]);
  expect(sim.groups.map(({ name, size, tone }) => [name, size, tone])).toEqual([["src/cache", 5, 0], ["src/daemon", 3, 1], ["Lone", 1, null]]);
});

it("groups documents by their category, and the rest by community as uncategorised", () => {
  const doc = (id: string, category?: string) => ({ id, kind: "doc", title: id, paths: ["src/x"], ...(category ? { category } : {}) }) as GraphNode;
  const g = docGraph([doc("s0", "Security"), doc("d0", "Daemon & performance"), doc("s1", "Security"), doc("u0"), doc("d1", "Daemon & performance"), doc("s2", "Security"), doc("u1")], []);
  const sim = createSim(g.nodes, g.edges);
  expect(sim.groups.map(({ name, size, tone }) => [name, size, tone])).toEqual([["Security", 3, 0], ["Daemon & performance", 2, 1], ["Uncategorised: src/x", 2, 2]]);
  expect(g.nodes.map((node, i) => `${node.id}:${sim.group[i]}`)).toEqual(["s0:0", "d0:1", "s1:0", "u0:2", "d1:1", "s2:0", "u1:2"]);
});

it("lets the legend reach every group past the first eight", () => {
  const nodes = Array.from({ length: 18 }, (_, i) => ({ id: `d${i}`, kind: "doc", title: `Doc ${i}`, paths: [`src/area${Math.floor(i / 2)}`] }) as GraphNode);
  render(<MapGraph nodes={nodes} edges={[]} onOpen={() => {}} />);
  expect(screen.getAllByRole("checkbox")).toHaveLength(8);
  fireEvent.click(screen.getByRole("button", { name: "+1 more" }));
  expect(screen.getAllByRole("checkbox")).toHaveLength(9);
});

it("highlights a hovered node's neighbourhood, selects on click, moves a dragged node without opening it, and zooms and fits", () => {
  const nodes: GraphNode[] = [{ id: "p", kind: "path", path: "src/cache" }, { id: "d", kind: "doc", title: "Cache design" }, { id: "e", kind: "doc", title: "Cache notes" }, { id: "x", kind: "doc", title: "Unrelated" }];
  const onOpen = vi.fn();
  const { container } = render(<MapGraph nodes={nodes} edges={[{ source: "d", target: "p", kind: "mentions" }, { source: "e", target: "p", kind: "mentions" }]} onOpen={onOpen} />);
  const node = (id: string) => container.querySelector<HTMLElement>(`[data-node="${id}"]`)!;
  expect(node("p")).toBeNull();
  fireEvent.mouseEnter(node("d"));
  expect([node("d").style.opacity, node("e").style.opacity, node("x").style.opacity]).toEqual(["1", "1", "0.2"]);
  expect(container.querySelector('[data-source="d"]')!.getAttribute("class")).toBe("text-branch");
  fireEvent.mouseLeave(node("d"));
  expect(node("x").style.opacity).toBe("1");
  // The pointer passing over another node leaves a focused node's light on.
  fireEvent.focus(node("d"));
  fireEvent.mouseEnter(node("x"));
  fireEvent.mouseLeave(node("x"));
  expect(node("x").style.opacity).toBe("0.2");
  fireEvent.blur(node("d"));
  expect(node("x").style.opacity).toBe("1");
  fireEvent.click(screen.getByRole("checkbox", { name: /src\/cache/ }));
  expect([node("d").style.opacity, node("e").style.opacity, node("x").style.opacity]).toEqual(["1", "1", "0.2"]);
  fireEvent.click(screen.getByRole("checkbox", { name: /src\/cache/ }));
  expect(node("x").style.opacity).toBe("1");

  fireEvent.click(node("d"));
  expect(onOpen).toHaveBeenCalledWith(nodes[1]);
  const panel = screen.getByRole("complementary", { name: "Node details" });
  expect(panel.textContent).toContain("src/cache");
  // A doc with no recorded provenance gets no provenance lines.
  expect(panel.querySelectorAll("[data-provenance]")).toHaveLength(0);
  // The panel's lists scroll; only the graph zooms.
  const stage = screen.getByTestId("graph-stage"), before = stage.style.transform;
  expect(fireEvent.wheel(panel, { deltaY: 100 })).toBe(true);
  expect(stage.style.transform).toBe(before);
  expect(fireEvent.wheel(stage, { deltaY: 100 })).toBe(false);
  expect(stage.style.transform).not.toBe(before);
  // A path listed in the panel narrows the map, as a path node once did.
  fireEvent.click(screen.getByRole("button", { name: "Code area: src/cache" }));
  expect(onOpen).toHaveBeenLastCalledWith({ id: "path:src/cache", kind: "path", path: "src/cache" });
  fireEvent.click(screen.getByRole("button", { name: "Close details" }));
  expect(screen.queryByRole("complementary")).toBeNull();
  // Escape from a focused node dismisses a selection (consumed); with none left it reaches the app's Esc-back.
  fireEvent.click(node("d"));
  expect(fireEvent.keyDown(node("d"), { key: "Escape" })).toBe(false);
  expect(screen.queryByRole("complementary")).toBeNull();
  expect(fireEvent.keyDown(node("d"), { key: "Escape" })).toBe(true);

  const left = parseFloat(node("x").style.left);
  fireEvent.pointerDown(node("x"), { pointerId: 1, clientX: 10, clientY: 10 });
  fireEvent.pointerMove(node("x"), { pointerId: 1, clientX: 70, clientY: 10 });
  fireEvent.pointerUp(node("x"), { pointerId: 1, clientX: 70, clientY: 10 });
  fireEvent.click(node("x"), { detail: 1 });
  expect(parseFloat(node("x").style.left)).toBeGreaterThan(left + 20);
  expect(onOpen).toHaveBeenCalledTimes(3);

  fireEvent.click(screen.getByRole("button", { name: "Fit graph" }));
  const fitted = stage.style.transform;
  fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
  expect(stage.style.transform).not.toBe(fitted);
  fireEvent.click(screen.getByRole("button", { name: "Fit graph" }));
  expect(stage.style.transform).toBe(fitted);
});
