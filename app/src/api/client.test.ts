import { describe, expect, it } from "vitest";
import { DaemonClient, DaemonError } from "./client";
import { BASE_URL, FakeDaemon, freshProviders } from "../test/fakeDaemon";
import { REPO, root, worker } from "../test/fixtures";

describe("DaemonClient", () => {
  it("sends the token and the repo, and returns the route's JSON", async () => {
    const daemon = new FakeDaemon({ workspaces: [root, worker, { ...root, id: "other", path: "/elsewhere" }] });
    const snapshot = await daemon.client().snapshot(REPO);
    expect(snapshot.map((ws) => ws.id)).toEqual(["proj", "proj/feat-ls"]);
    expect(daemon.requests).toEqual([{ method: "GET", path: "/snapshot", query: { repo: REPO } }]);
  });

  it.each([
    ["a wrong token", (d: FakeDaemon) => d.client("nope").meta(), "unauthorized", 401],
    ["an engine error", (d: FakeDaemon) => d.client().agent("ghost"), "not_found", 404],
    ["a missing required query", (d: FakeDaemon) => d.client().sessions(""), "invalid", 400],
    [
      "no daemon",
      () => new DaemonClient({ baseUrl: BASE_URL, token: "t", fetch: () => Promise.reject(new TypeError("refused")) }).meta(),
      "unreachable",
      0,
    ],
  ])("throws a DaemonError for %s", async (_case, call, code, status) => {
    const err = await call(new FakeDaemon()).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(DaemonError);
    expect(err).toMatchObject({ code, status });
  });

  it.each([
    ["the routing's supervisor choice", { routingConfig: { user: { supervisor: "chat-codex" }, repo: {}, local: {} } }, null],
    ["the remembered pick", { supervisorPick: "codex" }, null],
    // Set in the local scope: the choice lands in the user's file, its fallbacks in the local one, and
    // OpenCode isn't installed, so its Codex fallback is the one that runs.
    ["a fallback set in another scope than its choice", {}, { model: "chat-opencode", fallbacks: ["chat-codex"] }],
  ])("starts a provider-less session on %s over Claude when both are usable", async (_case, data, supervisor) => {
    const both = freshProviders({ claude: { path: "/bin/claude", version: "2" }, codex: { path: "/bin/codex", version: "1" } });
    const daemon = new FakeDaemon({
      repos: [{ repo_root: REPO, name: "proj" }],
      workspaces: [{ ...root, agents: [] }],
      providers: both,
      routingProfiles: [
        { name: "chat-codex", provider: "codex", model: "gpt-x", cost_tier: "medium", installed: true, signed_in: true },
        { name: "chat-opencode", provider: "opencode", model: "oc-x", cost_tier: "medium", installed: false, signed_in: false },
      ],
      ...data,
    });
    if (supervisor) await daemon.client().putRouting({ repo: REPO, scope: "local", kinds: { supervisor } });
    const usable = await (await daemon.fetch(`${BASE_URL}/v1/providers/usable?repo=${encodeURIComponent(REPO)}`, { headers: { authorization: `Bearer ${daemon.token}` } })).json();
    expect(usable).toEqual({ usable: ["codex", "claude"], default: "codex" });
    await daemon.client().startSession({ repo: REPO });
    expect(daemon.data.workspaces.flatMap((ws) => ws.agents ?? []).map((a) => a.provider)).toEqual(["codex"]);
  });

  it("addresses a workspace whose id has a slash", async () => {
    const diff = await new FakeDaemon({ workspaces: [worker] }).client().diff("proj/feat-ls");
    expect(diff).toMatchObject({ workspace_id: "proj/feat-ls", base: "main" });
  });

  it("builds the events URL with since and repo but never the token", () => {
    const url = new URL(new DaemonClient({ baseUrl: `${BASE_URL}/`, token: "secret" }).eventsUrl(42, REPO));
    expect(`${url.protocol}//${url.host}${url.pathname}`).toBe("ws://127.0.0.1:47321/v1/events");
    expect(Object.fromEntries(url.searchParams)).toEqual({ since: "42", repo: REPO });
    expect(url.toString()).not.toContain("secret");
  });
});
