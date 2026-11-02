import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DaemonClient } from "../../api/client";
import type { DecisionDTO } from "../../api/schema";
import { MapView } from "./MapView";

afterEach(cleanup);

const decision: DecisionDTO = { id: "private-d", repo_key: "r", topic: "Storage policy", body: "Keep private notes here", rationale: "Local reason", scope: ["src/cache/**"], status: "accepted", created_at: 1, author_session: "local-session" };

it("loads all docs automatically with sources, filters by path, and opens each node's target", async () => {
  const requests: { path: string; query: Record<string, string>; method?: string }[] = [];
  const scopes = [{ kind: "user", id: "u1" }, { kind: "group", id: "team-1" }, { kind: "org", id: "org-1" }];
  const fetch = vi.fn<typeof globalThis.fetch>(async (input, init) => {
    const url = new URL(String(input));
    const path = url.pathname.replace(/^\/v1/, "");
    requests.push({ path, query: Object.fromEntries(url.searchParams), method: init?.method });
    const body = path === "/analysis-docs/scopes" ? { scopes }
      : path === "/analysis-docs/map" ? { nodes: [{ id: "p", kind: "path", path: "src/cache/index.ts" }, { id: "d", kind: "doc", doc_id: "doc1", title: "Cache design", paths: ["src/cache/index.ts"], source: { kind: "task", ref: "feat/cache", session: "session-1" }, created_by: "user-hannah", scope: scopes.find((s) => s.id === url.searchParams.get("scope_id")), created_at: 1793610000 }, { id: "old", kind: "doc", title: "Old design" }], edges: [{ source: "d", target: "old", kind: "supersedes" }] }
      : path === "/analysis-docs/doc1" ? { id: "doc1", title: "Cache design", files: [{ name: "design.md", md: "# Durable cache" }] } : null;
    if (!body) throw new Error(`Unexpected request: ${path}`);
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  const client = new DaemonClient({ baseUrl: "http://localhost", token: "t", fetch });
  const openPath = vi.fn(async () => {});
  const props = {
    client, repo: "/repo", openPath,
    decisions: [decision, ...["src/*", "src/cache/index.?s", "src/cache/[ij]ndex.ts", "src/cache/[!a-h]ndex.ts", "cache/**", "./src//cache/../cache/index.ts"].map((pattern, i) => ({ ...decision, id: `pattern-${i}`, topic: pattern, scope: [pattern] })), { ...decision, id: "unrelated", topic: "Other rule", scope: ["src/cache-other/**", "src/cache/index.?x", "src/cache/[!ij]ndex.ts", "src/cache"] }, { ...decision, id: "global", topic: "Repo rule", scope: [] }],
    sources: [{ id: "s", kind: "folder", scope: "/repo/src/cache", docs: 1, added_at: 1793610000, visibility: "team" }, { id: "empty", kind: "folder", scope: "/repo", docs: 0 }, { id: "literal", kind: "folder", scope: "/repo/src/*", docs: 1 }, { id: "outside", kind: "folder", scope: "/outside/src/cache" }],
  };
  const { container, rerender } = render(<MapView {...props} decisions={[]} sources={[]} />);
  await screen.findByRole("option", { name: "Mine" });
  const initialDoc = await screen.findByRole("button", { name: /Cache design/ });
  expect(requests.at(-1)?.query.paths).toBe("[]");
  expect(initialDoc.textContent).toMatch(/feat\/cache.*session-1.*user-hannah.*Personal.*2026-11-02/);
  fireEvent.change(screen.getByLabelText("Code paths"), { target: { value: "src/cache" } });
  fireEvent.click(screen.getByRole("button", { name: "Explore area" }));
  await screen.findByRole("button", { name: /Cache design/ });
  rerender(<MapView {...props} />);
  await screen.findByRole("button", { name: /Storage policy/ });
  expect(screen.getByRole("button", { name: /Storage policy/ }).textContent).toMatch(/local-session.*1970-01-01/);
  expect(screen.getByRole("button", { name: /Imported.*cache/ }).textContent).toMatch(/\/repo\/src\/cache.*Team.*2026-11-02/);
  expect(container.querySelector('[data-source="local:decision:private-d"][data-target="p"]')).not.toBeNull();
  expect(container.querySelector('[data-source="local:source:s"][data-target="p"]')).not.toBeNull();
  expect(screen.queryByRole("button", { name: /Imported.*index.ts/ })).toBeNull();
  expect(screen.queryByRole("button", { name: /Imported.*\*/ })).toBeNull();
  for (let i = 0; i < 6; i++) expect(container.querySelector(`[data-source="local:decision:pattern-${i}"][data-target="p"]`)).not.toBeNull();
  expect(container.querySelector('[data-kind="supersedes"]')).not.toBeNull();
  expect(screen.queryByRole("button", { name: /Other rule|Repo rule|outside/ })).toBeNull();
  expect(requests.filter((r) => r.path.endsWith("/map")).at(-1)?.query).toEqual({ repo: "/repo", scope_kind: "user", scope_id: "u1", paths: '["src/cache"]', depth: "1" });
  expect(requests.filter((r) => r.path.endsWith("/map"))).toHaveLength(2);
  expect(JSON.stringify(requests)).not.toMatch(/private-d|Keep private|Local reason|\/repo\/src\/cache/);
  fireEvent.click(screen.getByRole("button", { name: /Storage policy/ }));
  expect(screen.getByText(decision.body)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /Imported.*cache/ }));
  expect(openPath).toHaveBeenLastCalledWith("/repo/src/cache");
  fireEvent.click(screen.getByRole("button", { name: /Imported.*localrepo/ }));
  expect(openPath).toHaveBeenLastCalledWith("/repo");
  rerender(<MapView {...props} sources={[{ id: "root-source", kind: "folder", scope: "/repo" }]} />);
  expect(screen.getByRole("button", { name: /Imported.*localrepo/ })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /Document.*Cache design/ }));
  expect((await screen.findByTestId("doc-body")).textContent).toContain("Durable cache");
  expect(requests.at(-1)?.path).toBe("/analysis-docs/doc1");
  fireEvent.click(screen.getByRole("button", { name: /Code area.*src\/cache\/index.ts/ }));
  await waitFor(() => expect(requests.filter((r) => r.path.endsWith("/map"))).toHaveLength(3));
  expect(requests.at(-1)?.query.paths).toBe('["src/cache/index.ts"]');
  fireEvent.change(screen.getByLabelText("Code paths"), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Explore area" }));
  await waitFor(() => expect(requests.at(-1)?.query.paths).toBe("[]"));
  for (const scope of scopes.slice(1)) {
    fireEvent.change(screen.getByLabelText("Library scope"), { target: { value: `${scope.kind}:${scope.id}` } });
    fireEvent.click(screen.getByRole("button", { name: "Explore area" }));
    const node = await screen.findByRole("button", { name: /Cache design/ });
    expect(node.textContent).toContain(scope.id);
    expect(requests.at(-1)?.query.scope_id).toBe(scope.id);
  }
});

it("keeps an in-flight area request on a second trigger and discards it when the repo changes", async () => {
  const requests: URL[] = [];
  let finish!: (response: Response) => void;
  const client = new DaemonClient({ baseUrl: "http://localhost", token: "t", fetch: async (input) => {
    const url = new URL(String(input));
    requests.push(url);
    if (url.pathname.endsWith("/scopes")) return new Response(JSON.stringify({ scopes: [{ kind: "user", id: "u1" }] }));
    return new Promise<Response>((resolve) => { finish = resolve; });
  } });
  const props = { client, decisions: [], sources: [] };
  const { rerender } = render(<MapView {...props} repo="/one" />);
  await screen.findByRole("option");
  fireEvent.change(screen.getByLabelText("Code paths"), { target: { value: "src/cache" } });
  fireEvent.click(screen.getByRole("button", { name: "Explore area" }));
  fireEvent.click(screen.getByRole("button", { name: "Explore area" }));
  expect(requests.filter((r) => r.pathname.endsWith("/map"))).toHaveLength(2);
  const finishPrevious = finish;
  rerender(<MapView {...props} repo="/two" />);
  await screen.findByRole("option");
  await act(async () => { finishPrevious(new Response(JSON.stringify({ nodes: [{ id: "old", kind: "doc", title: "Previous repo" }], edges: [] }))); });
  expect(screen.queryByRole("status")).not.toBeNull();
  expect(requests.filter((r) => r.pathname.endsWith("/map"))).toHaveLength(3);
  expect(screen.queryByRole("button", { name: /Previous repo/ })).toBeNull();
});
