import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { StrictMode, createElement, type ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { DaemonError, type DaemonClient, type SharedDesign } from "../api/client";
import { DaemonContext } from "../api/context";
import type { Route } from "../routes";
import type { ShareLinkDesignClient } from "../screens/Session/Visuals/client";
import { LibraryDesign } from "../screens/Session/Visuals/LibraryDesign";
import { SharedLinkReader } from "../screens/Session/Visuals/SharedLinkReader";
import { createAppStore } from "../store/store";
import { StoreContext } from "../store/useStore";
import { DeepLinkInbox, deepLinkRoute, parseDeepLink, useDeepLinks, type DeepLinkSource } from "./deeplink";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const TOKEN = "dsl_" + "Ab3_-".repeat(8) + "xyz";

const plan: SharedDesign = {
  id: "link_0123456789abcdef", session: "", title: "Cloud workers", rev: 3, state: "open", markdown: "Why.", lede: "Why.",
  sections: [{ id: "s", title: "Summary", markdown: "Workers run here." }], decisions: [], split: [], queued: 0, created_at: 0,
  me: "u_me", author: null, can_comment: false, can_suggest: false, can_sign_off: false, signed_off: false, gate: null,
};

it.each([
  [`brindle://plan/b/${TOKEN}`, { org: null, team: null, plan: null }],
  [`brindle://plan/acme/checkout-latency/${TOKEN}`, { org: "acme", team: null, plan: "checkout-latency" }],
  [`brindle://plan/acme/payments/checkout-latency/${TOKEN}`, { org: "acme", team: "payments", plan: "checkout-latency" }],
  [`brindle://plan/b/${TOKEN}/`, { org: null, team: null, plan: null }],
])("reads the plan a link names: %s", (raw, slugs) => {
  expect(parseDeepLink(raw)).toEqual({ kind: "plan", token: TOKEN, ...slugs });
});

it.each([
  ["another scheme", `https://plans.pawdelta.com/b/${TOKEN}`],
  ["another kind of brindle link", "brindle://visual/v_1"],
  ["a token cut short", `brindle://plan/b/${TOKEN.slice(0, -1)}`],
  ["a token that isn't one", `brindle://plan/b/abc_${"x".repeat(43)}`],
  ["two segments without b", `brindle://plan/acme/${TOKEN}`],
  ["too many segments", `brindle://plan/a/b/c/d/${TOKEN}`],
  ["a query", `brindle://plan/b/${TOKEN}?next=1`],
  ["a path that climbs", `brindle://plan/../b/${TOKEN}`],
  ["an empty segment", `brindle://plan//x/${TOKEN}`],
  ["not a string", 42],
])("refuses %s", (_why, raw) => {
  expect(parseDeepLink(raw)).toBeNull();
});

/* The shell's queue, as api/tauri.ts reads it: links waiting, and a nudge for the next ones once
   it listens (`listening`: when that registration finishes; at once by default). */
function shell(waiting: string[], listening: Promise<void> = Promise.resolve()) {
  let nudge = () => {};
  const source: DeepLinkSource = {
    take: async () => waiting.splice(0),
    subscribe: (on) => listening.then(() => { nudge = on; }),
  };
  return { inbox: new DeepLinkInbox(source), waiting, arrive: (raw: string) => act(async () => { waiting.push(raw); nudge(); }) };
}

function Opener({ inbox, onRoute }: { inbox: DeepLinkInbox; onRoute(r: Route): void }) {
  useDeepLinks(inbox, onRoute, () => ({ screen: "inbox" }));
  return null;
}

it("a link waiting at start opens once, though React runs the effect twice", async () => {
  const routes: Route[] = [];
  const { inbox, waiting } = shell([`brindle://plan/b/${TOKEN}`]);
  await act(async () => { render(createElement(StrictMode, null, createElement(Opener, { inbox, onRoute: (r: Route) => routes.push(r) }))); });
  expect(routes.map((r) => r.screen === "plan-link" && r.link?.token)).toEqual([TOKEN]);
  expect(waiting).toEqual([]);
});

it("a link that comes while the app starts listening still opens", async () => {
  let ready!: () => void;
  const routes: Route[] = [];
  const { inbox, waiting } = shell([], new Promise<void>((resolve) => { ready = resolve; }));
  await act(async () => { render(createElement(Opener, { inbox, onRoute: (r: Route) => routes.push(r) })); });
  // Queued by the shell before anything listened: no nudge reaches the page.
  waiting.push(`brindle://plan/b/${TOKEN}`);
  await act(async () => ready());
  expect(routes).toHaveLength(1);
});

function withApp(children: ReactNode, client: Partial<DaemonClient> = {}) {
  const store = createAppStore();
  const tree = createElement(StoreContext.Provider, { value: store },
    createElement(DaemonContext.Provider, { value: client as DaemonClient }, children));
  return { store, tree };
}

it("rejects an invalid link without side effects: the calm page, and nothing read, fetched or opened", async () => {
  const fetching = vi.spyOn(globalThis, "fetch");
  const opening = vi.spyOn(window, "open").mockReturnValue(null);
  const routes: Route[] = [];
  const { inbox, arrive } = shell([]);
  await act(async () => { render(createElement(Opener, { inbox, onRoute: (r: Route) => routes.push(r) })); });
  await arrive("brindle://visual/../../v1/agents?kill=1");
  expect(routes).toEqual([{ screen: "plan-link", link: null, from: { screen: "inbox" } }]);

  const client: ShareLinkDesignClient = { shareLinkDesign: vi.fn(async () => plan) };
  const route = routes[0] as Extract<Route, { screen: "plan-link" }>;
  await act(async () => { render(createElement(SharedLinkReader, { client, link: route.link, onBack: () => {} })); });
  expect(screen.getByTestId("deep-link-invalid")).toBeTruthy();
  expect(client.shareLinkDesign).not.toHaveBeenCalled();
  expect(fetching).not.toHaveBeenCalled();
  expect(opening).not.toHaveBeenCalled();
});

it("opens a plan link through the link, read only, and Back goes to where the person was", async () => {
  const routes: Route[] = [];
  const { inbox } = shell([`brindle://plan/acme/cloud-workers/${TOKEN}`]);
  await act(async () => { render(createElement(Opener, { inbox, onRoute: (r: Route) => routes.push(r) })); });
  const route = routes[0] as Extract<Route, { screen: "plan-link" }>;
  expect(route.link?.token).toBe(TOKEN);
  // From one link's page to the next, Back still goes to where the person was before either.
  expect(deepLinkRoute(`brindle://plan/b/${TOKEN}`, route)).toMatchObject({ from: { screen: "inbox" } });
  cleanup();

  const read = vi.fn(async (_token: string) => plan);
  const back = vi.fn();
  await act(async () => { render(createElement(SharedLinkReader, { client: { shareLinkDesign: read }, link: route.link, onBack: back })); });
  expect(screen.getByText("Workers run here.")).toBeTruthy();
  expect(read.mock.calls.every(([token]) => token === TOKEN)).toBe(true);
  expect(screen.queryByRole("button", { name: "Comment" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /Back/ }));
  expect(back).toHaveBeenCalled();
});

it("a link to a design shared with this account opens it as the Library does, with its rights; a refused one opens nothing", async () => {
  // The daemon answers with the Library's own doc id only for someone the design is shared with.
  const design: SharedDesign = {
    ...plan, id: "doc_1", author: "u_author", can_edit: true, can_comment: true, can_suggest: true, can_sign_off: true,
    gate: { state: "waiting", required: 1, signed: 0, waiting_on: ["u_me"], reviewers: [{ sub: "u_me", required: true, status: "waiting", you: true }] },
  };
  let read: () => Promise<SharedDesign> = async () => design;
  const sharedDesign = vi.fn(async () => ({ ...design, title: "Design doc" }));
  const client = {
    shareLinkDesign: () => read(),
    libraryDesignPlan: async () => { throw new DaemonError("not_found", "not shared from here", 404); },
    designWorkItems: async () => { throw new DaemonError("not_found", "none", 404); },
    sharedDesign, writeSharedDesign: async () => design, libraryPeople: async () => ({ people: [] }),
    designShareLink: async () => ({ available: true, link: null }), copyDesignShareLink: async () => ({ available: true, link: null }),
    revokeDesignShareLink: async () => ({ available: true, link: null }),
  } as unknown as DaemonClient;
  const open = () => createElement(SharedLinkReader, {
    client, link: parseDeepLink(`brindle://plan/acme/payments/cloud-workers/${TOKEN}`), onBack: () => {},
    member: (d: { id: string; title: string }) => createElement(LibraryDesign, { client, docId: d.id, title: d.title, onBack: () => {} }),
  });
  await act(async () => { render(open()); });
  expect(sharedDesign).toHaveBeenCalledWith("doc_1", undefined);
  expect(screen.getByRole("heading", { level: 1, name: "Cloud workers" })).toBeTruthy();       // the link's name for it
  expect((screen.getByRole("radio", { name: /Suggesting/ }) as HTMLButtonElement).disabled).toBe(false);
  expect(screen.getByRole("button", { name: "Share" })).toBeTruthy();                         // an editor's
  cleanup();

  read = async () => { throw new DaemonError("not_found", "This link doesn't open a plan for this account.", 404); };
  await act(async () => { render(open()); });
  expect(screen.getByTestId("deep-link-failed")).toBeTruthy();
  expect(screen.queryByText("Workers run here.")).toBeNull();
});

it("signed out, or the sign-in lost while the plan is open, asks the person to sign in, then opens the plan", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  let signedIn = false;
  const read = vi.fn(async () => {
    if (!signedIn) throw new DaemonError("signed_out", "sign in to brindle to open this link", 402);
    return plan;
  });
  let jobs = 0;
  const { store, tree } = withApp(
    createElement(SharedLinkReader, { client: { shareLinkDesign: read }, link: parseDeepLink(`brindle://plan/b/${TOKEN}`), onBack: () => {} }),
    { accountLogin: async () => ({ job: `j${++jobs}` }) as never },
  );
  const signIn = async () => {
    expect(screen.getByTestId("deep-link-sign-in")).toBeTruthy();
    expect(screen.queryByText("Workers run here.")).toBeNull();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Sign in" })));
    signedIn = true;
    await act(async () => {
      store.dispatch({ type: "stream", message: { type: "event", id: jobs, ts: 0, kind: "job.done", repo_root: null, root_id: null, agent_id: null, workspace_id: null, task_id: null, payload: { job: `j${jobs}` } } });
    });
    expect(screen.getByText("Workers run here.")).toBeTruthy();
  };
  try {
    await act(async () => { render(tree); });
    await signIn();
    // Open, then a later read finds the sign-in gone: the same sign-in, and the plan again after it.
    signedIn = false;
    await act(async () => { vi.advanceTimersByTime(30_000); });
    await signIn();
  } finally {
    vi.useRealTimers();
  }
});

it("a late 402 from an obsolete read undoes neither a newer read nor a sign-in since", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  const calls: { ok(): void; signedOut(): void }[] = [];
  const read = vi.fn(() => new Promise<SharedDesign>((resolve, reject) => {
    calls.push({ ok: () => resolve(plan), signedOut: () => reject(new DaemonError("signed_out", "sign in", 402)) });
  }));
  const { store, tree } = withApp(
    createElement(SharedLinkReader, { client: { shareLinkDesign: read }, link: parseDeepLink(`brindle://plan/b/${TOKEN}`), onBack: () => {} }),
    { accountLogin: async () => ({ job: "j1" }) as never },
  );
  const poll = () => act(async () => { vi.advanceTimersByTime(30_000); });
  const shown = () => !!screen.queryByText("Workers run here.");
  try {
    await act(async () => { render(tree); });
    await act(async () => calls[0].ok());                   // the first read: the reader opens, its own read goes out (1)
    await poll();                                           // a newer read (2) answers first
    await act(async () => calls[2].ok());
    expect(shown()).toBe(true);
    await act(async () => calls[1].signedOut());            // the older one's 402, late: the plan stays
    expect(shown()).toBe(true);
    expect(screen.queryByTestId("deep-link-sign-in")).toBeNull();

    await poll();                                           // a read (3) left out ...
    await poll();
    await act(async () => calls[4].signedOut());            // ... while the newest (4) finds the sign-in gone
    expect(screen.getByTestId("deep-link-sign-in")).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Sign in" })));
    await act(async () => {
      store.dispatch({ type: "stream", message: { type: "event", id: 1, ts: 0, kind: "job.done", repo_root: null, root_id: null, agent_id: null, workspace_id: null, task_id: null, payload: { job: "j1" } } });
    });
    await act(async () => calls[5].ok());
    await act(async () => calls[6].ok());
    expect(shown()).toBe(true);
    await act(async () => calls[3].signedOut());            // from before the sign-in: the plan stays
    expect(shown()).toBe(true);
    expect(screen.queryByTestId("deep-link-sign-in")).toBeNull();
  } finally {
    vi.useRealTimers();
  }
});
