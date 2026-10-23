import { describe, expect, it } from "vitest";
import { DaemonClient, DaemonError } from "./client";
import { BASE_URL, FakeDaemon } from "../test/fakeDaemon";
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
