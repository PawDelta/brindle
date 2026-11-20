import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { DaemonError, type DaemonClient, type SharedDesign } from "../api/client";
import { DaemonContext } from "../api/context";
import type { Route } from "../routes";
import type { ShareLinkDesignClient } from "../screens/Session/Visuals/client";
import { SharedLinkReader } from "../screens/Session/Visuals/SharedLinkReader";
import { createAppStore } from "../store/store";
import { StoreContext } from "../store/useStore";
import { deepLinkRoute, parseDeepLink, useDeepLinks, type DeepLinkSource } from "./deeplink";

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

/* The shell's queue, as api/tauri.ts reads it: links waiting, and a nudge for the next ones. */
function shell(waiting: string[]) {
  let nudge = () => {};
  const source: DeepLinkSource = {
    take: async () => waiting.splice(0),
    subscribe: (on) => { nudge = on; return () => { nudge = () => {}; }; },
  };
  return { source, arrive: (raw: string) => act(async () => { waiting.push(raw); nudge(); }) };
}

function Opener({ source, onRoute }: { source: DeepLinkSource; onRoute(r: Route): void }) {
  useDeepLinks(source, onRoute, () => ({ screen: "inbox" }));
  return null;
}

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
  const { source, arrive } = shell([]);
  await act(async () => { render(createElement(Opener, { source, onRoute: (r: Route) => routes.push(r) })); });
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
  const { source } = shell([`brindle://plan/acme/cloud-workers/${TOKEN}`]);
  await act(async () => { render(createElement(Opener, { source, onRoute: (r: Route) => routes.push(r) })); });
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

it("signed out, asks the person to sign in, then opens the plan", async () => {
  let signedIn = false;
  const read = vi.fn(async () => {
    if (!signedIn) throw new DaemonError("signed_out", "sign in to brindle to open this link", 402);
    return plan;
  });
  const { store, tree } = withApp(
    createElement(SharedLinkReader, { client: { shareLinkDesign: read }, link: parseDeepLink(`brindle://plan/b/${TOKEN}`), onBack: () => {} }),
    { accountLogin: async () => ({ job: "j1" }) as never },
  );
  await act(async () => { render(tree); });
  expect(screen.getByTestId("deep-link-sign-in")).toBeTruthy();
  expect(screen.queryByText("Workers run here.")).toBeNull();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Sign in" })));
  signedIn = true;
  await act(async () => {
    store.dispatch({ type: "stream", message: { type: "event", id: 1, ts: 0, kind: "job.done", repo_root: null, root_id: null, agent_id: null, workspace_id: null, task_id: null, payload: { job: "j1" } } });
  });
  expect(screen.getByText("Workers run here.")).toBeTruthy();
});
