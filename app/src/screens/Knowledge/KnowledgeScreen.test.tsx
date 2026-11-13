import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DecisionDTO } from "../../api/schema";
import { SessionsProvider } from "../../store/sessions";
import { createAppStore } from "../../store/store";
import { startSync } from "../../store/sync";
import { StoreContext } from "../../store/useStore";
import { DaemonClient } from "../../api/client";
import { BASE_URL, FakeDaemon, TOKEN, type DaemonData } from "../../test/fakeDaemon";
import { REPO } from "../../test/fixtures";
import { KnowledgeScreen } from ".";

afterEach(cleanup);

const decision = (id: string, topic: string, body: string, scope: string[], rationale = ""): DecisionDTO => ({
  id: id.padEnd(32, "0"),
  repo_key: "proj",
  topic,
  body,
  rationale,
  scope,
  status: "accepted",
  created_at: Date.now() / 1000 - 3600,
});

function setup(data: Partial<DaemonData> = {}, pickFolder?: (title: string) => Promise<string | null>, view: "imported" | "decisions" = "decisions") {
  const daemon = new FakeDaemon({
    repos: [{ repo_root: REPO, name: "proj" }],
    decisions: [decision("aaaaaaaa", "storage", "use sqlite for the cache", ["src/cache/**"], "simple"), decision("bbbbbbbb", "logging", "structured logs only", [])],
    kbSources: [{ id: "f0e1d2c3b4a5968778695a4b3c2d1e0f", kind: "folder", scope: "/home/u/proj/docs", mode: "indexed", visibility: "team", docs: 14, quarantined: true, added_at: Date.now() / 1000 - 86400 }],
    kbHits: [
      { chunk_id: "c1", heading_path: "Deploys > Rollback", text: "Roll back with the previous image tag; the migrations are forward-only.", url: "file:///home/u/proj/docs/deploys.md", title: "Deploys", external_id: "docs/deploys.md", updated_at: Date.now() / 1000 - 400 * 86400, updated: "2025-09-01" },
    ],
    ...data,
  });
  const store = createAppStore();
  /* Paths the daemon refuses with a transport error, for the moment. */
  const failing = new Set<string>();
  const fetch: typeof globalThis.fetch = async (input, init) => {
    const url = new URL(String(input));
    if (url.pathname === "/v1/decisions" || url.pathname.startsWith("/v1/kb/")) {
      const repo = url.pathname === "/v1/decisions" && init?.method === "POST"
        ? JSON.parse(String(init.body)).repo : url.searchParams.get("repo");
      if (repo !== REPO) return new Response(JSON.stringify({ error: { code: "not_found" } }), { status: 404 });
    }
    return [...failing].some((p) => String(input).includes(p)) ? Promise.reject(new Error("index locked")) : daemon.fetch(input, init);
  };
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch });
  const sync = startSync({ client, store, connect: daemon.connect });
  const page = (at: "imported" | "decisions") => (
    <StoreContext.Provider value={store}>
      <SessionsProvider client={client}>
        <KnowledgeScreen client={client} pickFolder={pickFolder} view={at} />
      </SessionsProvider>
    </StoreContext.Provider>
  );
  const { rerender } = render(page(view));
  /* The Library's sidebar picks another view: the same screen follows. */
  return { daemon, failing, show: (at: "imported" | "decisions") => rerender(page(at)), stop: () => sync.stop() };
}

const listed = () => [...screen.getByTestId("decisions").querySelectorAll("li[data-decision]")].map((li) => li.getAttribute("data-topic"));

describe("knowledge screen", () => {
  it("searches knowledge and replaces a conflicting decision", async () => {
    const { daemon, failing, show, stop } = setup();
    await waitFor(() => expect(listed()).toEqual(["storage", "logging"]));
    show("imported");
    expect(screen.getByTestId("sources").querySelector('li[data-source="f0e1d2c3b4a5968778695a4b3c2d1e0f"]')).toBeTruthy();
    show("decisions");
    // Search matches a decision's topic, text or rationale, as the engine's --query does, and asks
    // the engine for the documents' best passages: here a rollback note no decision mentions.
    const box = screen.getByRole("searchbox", { name: "Search the Library" });
    fireEvent.change(box, { target: { value: "SIMPLE" } });
    expect(listed()).toEqual(["storage"]);
    await waitFor(() => expect(screen.getByTestId("hits").querySelector("p:not([role])")).not.toBeNull());
    fireEvent.change(box, { target: { value: "rollback" } });
    expect(listed()).toEqual([]);
    await waitFor(() => expect(screen.getByTestId("hits").querySelector('li[data-hit="c1"]')).toBeTruthy());
    // A search the engine refuses is said, not hidden.
    failing.add("/kb/search");
    fireEvent.change(box, { target: { value: "rollback image" } });
    await within(screen.getByTestId("hits")).findByRole("alert");
    failing.clear();
    fireEvent.change(box, { target: { value: "" } });
    await waitFor(() => expect(screen.queryByTestId("hits")).toBeNull());

    // Recording on a topic already decided for these paths is refused, naming the decision in the way; one click supersedes it.
    fireEvent.click(screen.getByRole("button", { name: "Record a decision" }));
    const form = screen.getByRole("form", { name: "Record a decision" });
    fireEvent.change(within(form).getByLabelText("Topic"), { target: { value: "Storage" } });
    fireEvent.change(within(form).getByLabelText("Decision"), { target: { value: "use postgres for the cache" } });
    fireEvent.change(within(form).getByLabelText("Why"), { target: { value: "it outgrew sqlite" } });
    fireEvent.change(within(form).getByLabelText("Paths"), { target: { value: "src/cache/**, src/db/**" } });
    fireEvent.click(within(form).getByRole("button", { name: "Record" }));
    await within(form).findByRole("alert");
    fireEvent.click(within(form).getByRole("button", { name: "Replace aaaaaaaa" }));
    await screen.findByRole("status");
    expect(daemon.requests.filter((r) => r.method === "POST" && r.path === "/decisions").at(-1)?.body).toMatchObject({ supersedes: "aaaaaaaa" });
    // The list is fetched again: the new decision in, the superseded one out.
    await waitFor(() => expect(listed()).toEqual(["logging", "Storage"]));
    expect(daemon.data.decisions.find((d) => d.topic === "Storage")).toMatchObject({
      repo_key: REPO, body: "use postgres for the cache", rationale: "it outgrew sqlite",
      scope: ["src/cache/**", "src/db/**"],
    });
    expect(screen.queryByRole("form", { name: "Record a decision" })).toBeNull();
    stop();
  });

  it("records the first decision from the empty state", async () => {
    const { daemon, show, stop } = setup({ decisions: [], kbSources: [] });
    const empty = await screen.findByTestId("empty-state");
    expect(within(empty).getByRole("button", { name: "Add folder" })).toBeTruthy();
    expect(screen.queryByRole("searchbox", { name: "Search the Library" })).toBeNull();
    expect(screen.queryByTestId("decisions")).toBeNull();
    fireEvent.click(within(empty).getByRole("button", { name: "Record a decision" }));
    expect(screen.queryByTestId("empty-state")).toBeNull();
    const form = screen.getByRole("form", { name: "Record a decision" });
    fireEvent.change(within(form).getByLabelText("Topic"), { target: { value: "logging" } });
    fireEvent.change(within(form).getByLabelText("Decision"), { target: { value: "structured logs only" } });
    fireEvent.click(within(form).getByRole("button", { name: "Record" }));
    await waitFor(() => expect(listed()).toEqual(["logging"]));
    expect(daemon.data.decisions).toHaveLength(1);
    expect(screen.queryByTestId("empty-state")).toBeNull();
    expect(screen.getByRole("searchbox", { name: "Search the Library" })).toBeTruthy();
    show("imported");
    expect(screen.getByTestId("sources").textContent).toContain("No documents yet");
    stop();
  });

  it("adds a folder from the native picker, re-indexes and removes one in a click, each through the engine's routes", async () => {
    const picker = vi.fn(() => Promise.resolve("/home/sam/Notes/runbooks"));
    const { daemon, stop } = setup({}, picker, "imported");
    const sources = await screen.findByTestId("sources");
    await waitFor(() => expect(sources.querySelector('li[data-source="f0e1d2c3b4a5968778695a4b3c2d1e0f"]')).toBeTruthy());
    // Add: the picker fills the path; Add posts it, as the team's; the job ends and the folder is listed, indexed.
    fireEvent.click(within(sources).getByRole("button", { name: "Add folder" }));
    const form = within(sources).getByRole("form", { name: "Add a folder" });
    fireEvent.click(within(form).getByRole("button", { name: "Choose folder…" }));
    await waitFor(() => expect(within(form).getByLabelText("Folder path")).toHaveProperty("value", "/home/sam/Notes/runbooks"));
    fireEvent.click(within(form).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(daemon.requests.filter((r) => r.method === "POST" && r.path === "/kb/sources")).toHaveLength(1));
    await waitFor(() => expect(sources.textContent).toContain("runbooks"));
    await waitFor(() => expect(screen.queryByTestId("source-pending")).toBeNull());
    expect(screen.queryByRole("form", { name: "Add a folder" })).toBeNull();
    // Re-index: the engine's job for that folder; the row says so until it ends.
    fireEvent.click(within(sources).getByRole("button", { name: "Re-index docs" }));
    await waitFor(() => expect(daemon.kbSyncs).toEqual(["f0e1d2c3b4a5968778695a4b3c2d1e0f"]));
    await waitFor(() => expect(screen.queryByTestId("source-pending")).toBeNull());
    // Remove: one click, no confirmation, the folder gone.
    fireEvent.click(within(sources).getByRole("button", { name: "Remove docs" }));
    await waitFor(() => expect(daemon.kbRemovals).toEqual(["f0e1d2c3b4a5968778695a4b3c2d1e0f"]));
    await waitFor(() => expect(sources.querySelector('li[data-source="f0e1d2c3b4a5968778695a4b3c2d1e0f"]')).toBeNull());
    expect(screen.queryByRole("dialog")).toBeNull();
    stop();
  });

  it("takes a typed path without a picker, keeps the form and its path up with the engine's refusal under it, even in a repo with nothing yet", async () => {
    const { daemon, failing, stop } = setup({ decisions: [], kbSources: [] });
    const empty = await screen.findByTestId("empty-state");
    fireEvent.click(within(empty).getByRole("button", { name: "Add folder" }));
    const sources = screen.getByTestId("sources");
    const form = within(sources).getByRole("form", { name: "Add a folder" });
    expect(within(form).queryByRole("button", { name: "Choose folder…" })).toBeNull();
    fireEvent.change(within(form).getByLabelText("Folder path"), { target: { value: "~/Notes/design" } });
    fireEvent.click(within(form).getByLabelText("Just for me"));
    failing.add("/kb/sources?");
    fireEvent.click(within(form).getByRole("button", { name: "Add" }));
    // Refused: the error shows, the form stays with the path typed, and the empty state doesn't come back over it.
    await screen.findByTestId("source-error");
    expect(screen.getByRole("form", { name: "Add a folder" })).toBe(form);
    expect(within(form).getByLabelText("Folder path")).toHaveProperty("value", "~/Notes/design");
    expect(screen.queryByTestId("empty-state")).toBeNull();
    // Taken: the form goes, the folder is listed as this person's own.
    failing.clear();
    fireEvent.click(within(form).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(daemon.requests.filter((r) => r.method === "POST" && r.path === "/kb/sources")).toHaveLength(1));
    expect(daemon.requests.find((r) => r.method === "POST" && r.path === "/kb/sources")?.body).toEqual({ path: "~/Notes/design", private: true });
    await waitFor(() => expect(screen.queryByRole("form", { name: "Add a folder" })).toBeNull());
    expect(screen.queryByTestId("source-error")).toBeNull();
    stop();
  });
});
