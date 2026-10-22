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

function setup(data: Partial<DaemonData> = {}, pickFolder?: (title: string) => Promise<string | null>) {
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
  const fetch: typeof globalThis.fetch = (input, init) =>
    [...failing].some((p) => String(input).includes(p)) ? Promise.reject(new Error("index locked")) : daemon.fetch(input, init);
  const client = new DaemonClient({ baseUrl: BASE_URL, token: TOKEN, fetch });
  const sync = startSync({ client, store, connect: daemon.connect });
  render(
    <StoreContext.Provider value={store}>
      <SessionsProvider client={client}>
        <KnowledgeScreen client={client} pickFolder={pickFolder} />
      </SessionsProvider>
    </StoreContext.Provider>,
  );
  return { daemon, failing, stop: () => sync.stop() };
}

const listed = () => [...screen.getByTestId("decisions").querySelectorAll("li[data-decision]")].map((li) => li.getAttribute("data-topic"));

describe("knowledge screen", () => {
  it("says what knowledge is, shows decisions as cards and documents as a list, searches both, and records a decision over the one in its way", async () => {
    const { daemon, failing, stop } = setup();
    const page = screen.getByRole("region", { name: "Knowledge" });
    expect(page.textContent).toContain("What your agents know and remember about this repo");
    const decisions = await screen.findByTestId("decisions");
    await waitFor(() => expect(listed()).toEqual(["storage", "logging"]));
    expect(daemon.requests.find((r) => r.path === "/decisions")?.query).toEqual({ repo: REPO });
    expect(daemon.requests.find((r) => r.path === "/kb/sources")?.query).toEqual({ repo: REPO });
    // A decision's card: what was decided, why, when, its state and the paths it covers (none: the whole repo).
    const storage = decisions.querySelector('li[data-topic="storage"]')!;
    expect(storage.textContent).toContain("use sqlite for the cache");
    expect(storage.textContent).toContain("Why: simple");
    expect(storage.textContent).toContain("In force");
    expect(storage.textContent).toContain("1h ago");
    expect(storage.textContent).toContain("src/cache/**");
    expect(decisions.querySelector('li[data-topic="logging"]')!.textContent).toContain("the whole repo");
    // A document folder: its name, where it is, what is indexed and when it was added; held-back text said in plain words.
    const sources = screen.getByTestId("sources");
    expect(sources.textContent).toContain("docs");
    expect(sources.textContent).toContain("/home/u/proj/docs");
    expect(sources.textContent).toContain("14 documents indexed");
    expect(sources.textContent).toContain("added 1d ago");
    expect(sources.textContent).toContain("some text held back");
    // No jargon anywhere on the page, and nobody sent to a terminal.
    expect(page.textContent).not.toMatch(/quarantin|chunk|terminal|brindle kb/i);

    // Search matches a decision's topic, text or rationale, as the engine's --query does, and asks
    // the engine for the documents' best passages: here a rollback note no decision mentions.
    const box = screen.getByRole("searchbox", { name: "Search knowledge" });
    fireEvent.change(box, { target: { value: "SIMPLE" } });
    expect(listed()).toEqual(["storage"]);
    expect(screen.getByTestId("decision-count").textContent).toBe("1 of 2 match");
    await waitFor(() => expect(screen.getByTestId("hits").textContent).toContain("nothing matches"));
    expect(daemon.requests.filter((r) => r.path === "/kb/search").at(-1)?.query).toEqual({ repo: REPO, q: "SIMPLE" });
    fireEvent.change(box, { target: { value: "rollback" } });
    expect(listed()).toEqual([]);
    await waitFor(() => expect(screen.getByTestId("hits").querySelector('li[data-hit="c1"]')).toBeTruthy());
    const hit = screen.getByTestId("hits").querySelector('li[data-hit="c1"]')!;
    expect(hit.textContent).toContain("Deploys");
    expect(hit.textContent).toContain("Rollback");
    expect(hit.textContent).toContain("forward-only");
    expect(hit.textContent).toContain("not updated in 180 days");
    expect(screen.getByTestId("hits").textContent).toContain("the best passage");
    // A search the engine refuses is said, not hidden.
    failing.add("/kb/search");
    fireEvent.change(box, { target: { value: "rollback image" } });
    await waitFor(() => expect(screen.getByTestId("hits").textContent).toContain("Could not search the documents: index locked"));
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
    const refusal = await within(form).findByRole("alert");
    expect(refusal.textContent).toContain("already covers these paths (aaaaaaaa)");
    expect(daemon.requests.filter((r) => r.method === "POST" && r.path === "/decisions").at(-1)?.body).toEqual({
      repo: REPO,
      topic: "Storage",
      decision: "use postgres for the cache",
      rationale: "it outgrew sqlite",
      paths: ["src/cache/**", "src/db/**"],
    });
    fireEvent.click(within(form).getByRole("button", { name: "Replace aaaaaaaa" }));
    await screen.findByRole("status");
    expect(daemon.requests.filter((r) => r.method === "POST" && r.path === "/decisions").at(-1)?.body).toMatchObject({ supersedes: "aaaaaaaa" });
    // The list is fetched again: the new decision in, the superseded one out.
    await waitFor(() => expect(listed()).toEqual(["logging", "Storage"]));
    expect(screen.queryByRole("form", { name: "Record a decision" })).toBeNull();
    stop();
  });

  it("explains how knowledge gets here when a repo has none yet, and records the first decision from there", async () => {
    const { daemon, stop } = setup({ decisions: [], kbSources: [] });
    const empty = await screen.findByTestId("empty-state");
    expect(empty.textContent).toContain("You record a decision");
    expect(empty.textContent).toContain("A session records one");
    expect(empty.textContent).toContain("You add a folder of documents");
    expect(empty.textContent).not.toMatch(/terminal|brindle kb/i);
    expect(within(empty).getByRole("button", { name: "Add folder" })).toBeTruthy();
    expect(screen.queryByRole("searchbox", { name: "Search knowledge" })).toBeNull();
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
    expect(screen.getByRole("searchbox", { name: "Search knowledge" })).toBeTruthy();
    expect(screen.getByTestId("sources").textContent).toContain("No documents yet");
    stop();
  });

  it("adds a folder from the native picker, re-indexes and removes one in a click, each through the engine's routes", async () => {
    const picker = vi.fn(() => Promise.resolve("/home/sam/Notes/runbooks"));
    const { daemon, stop } = setup({}, picker);
    const sources = await screen.findByTestId("sources");
    await waitFor(() => expect(sources.querySelector('li[data-source="f0e1d2c3b4a5968778695a4b3c2d1e0f"]')).toBeTruthy());
    // Add: the picker fills the path; Add posts it, as the team's; the job ends and the folder is listed, indexed.
    fireEvent.click(within(sources).getByRole("button", { name: "Add folder" }));
    const form = within(sources).getByRole("form", { name: "Add a folder" });
    fireEvent.click(within(form).getByRole("button", { name: "Choose folder…" }));
    await waitFor(() => expect(within(form).getByLabelText("Folder path")).toHaveProperty("value", "/home/sam/Notes/runbooks"));
    expect(picker).toHaveBeenCalledWith("Add a folder of documents");
    fireEvent.click(within(form).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(daemon.requests.filter((r) => r.method === "POST" && r.path === "/kb/sources")).toHaveLength(1));
    expect(daemon.requests.find((r) => r.method === "POST" && r.path === "/kb/sources")).toMatchObject({ query: { repo: REPO }, body: { path: "/home/sam/Notes/runbooks" } });
    await waitFor(() => expect(sources.textContent).toContain("runbooks"));
    await waitFor(() => expect(screen.queryByTestId("source-pending")).toBeNull());
    await waitFor(() => expect(sources.textContent).toContain("3 documents indexed"));
    expect(screen.queryByRole("form", { name: "Add a folder" })).toBeNull();
    // Re-index: the engine's job for that folder; the row says so until it ends.
    fireEvent.click(within(sources).getByRole("button", { name: "Re-index docs" }));
    await waitFor(() => expect(daemon.kbSyncs).toEqual(["f0e1d2c3b4a5968778695a4b3c2d1e0f"]));
    expect(daemon.requests.find((r) => r.path === "/kb/sources/f0e1d2c3b4a5968778695a4b3c2d1e0f/sync")?.query).toEqual({ repo: REPO });
    await waitFor(() => expect(screen.queryByTestId("source-pending")).toBeNull());
    // Remove: one click, no confirmation, the folder gone.
    fireEvent.click(within(sources).getByRole("button", { name: "Remove docs" }));
    await waitFor(() => expect(daemon.kbRemovals).toEqual(["f0e1d2c3b4a5968778695a4b3c2d1e0f"]));
    expect(daemon.requests.find((r) => r.method === "DELETE")).toMatchObject({ path: "/kb/sources/f0e1d2c3b4a5968778695a4b3c2d1e0f", query: { repo: REPO } });
    await waitFor(() => expect(sources.querySelector('li[data-source="f0e1d2c3b4a5968778695a4b3c2d1e0f"]')).toBeNull());
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(sources.textContent).toContain("1 folder");
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
    await waitFor(() => expect(screen.getByTestId("source-error").textContent).toContain("Adding ~/Notes/design: index locked"));
    expect(screen.getByRole("form", { name: "Add a folder" })).toBe(form);
    expect(within(form).getByLabelText("Folder path")).toHaveProperty("value", "~/Notes/design");
    expect(screen.queryByTestId("empty-state")).toBeNull();
    // Taken: the form goes, the folder is listed as this person's own.
    failing.clear();
    fireEvent.click(within(form).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(daemon.requests.filter((r) => r.method === "POST" && r.path === "/kb/sources")).toHaveLength(1));
    expect(daemon.requests.find((r) => r.method === "POST" && r.path === "/kb/sources")?.body).toEqual({ path: "~/Notes/design", private: true });
    await waitFor(() => expect(screen.queryByRole("form", { name: "Add a folder" })).toBeNull());
    await waitFor(() => expect(sources.textContent).toContain("just for me"));
    expect(screen.queryByTestId("source-error")).toBeNull();
    stop();
  });
});
