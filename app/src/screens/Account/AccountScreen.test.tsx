import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionsProvider } from "../../store/sessions";
import { createAppStore } from "../../store/store";
import { startSync } from "../../store/sync";
import { StoreContext } from "../../store/useStore";
import { FakeDaemon } from "../../test/fakeDaemon";
import { REPO } from "../../test/fixtures";
import { AccountScreen } from ".";
import { ACCOUNT_URL, DOCS_URL, ENTERPRISE_URL, PRICING_URL } from "./account";

afterEach(cleanup);

function setup() {
  // A Pro entitlement with every Pro feature; the Team org below has only part of its plan's.
  const pro = { signed_in: true, name: "sam@example.com", plan: "pro", org_id: "org_sam", status: "active", features: ["learning", "cost", "services", "multi_repo", "coordinator", "settings_sync", "federation", "ci_fix"], seats: 1, role: "owner", expires_at: 1_800_000_000, in_grace: false, grace_until: null, learning: true, learning_note: "not enough similar tasks yet: kept the default" };
  // Under the team org the engine lists the account's orgs; under the personal one it couldn't (offline).
  const listed = [
    { id: "org_sam", name: "Sam", role: "owner", plan: "pro", personal: true, current: false },
    { id: "org_acme", name: "Acme", role: "member", plan: "team", personal: false, current: true },
  ];
  const daemon = new FakeDaemon({
    repos: [{ repo_root: REPO, name: "proj" }],
    account: { signed_in: false },
    accountAfterLogin: pro,
    orgs: {
      // A team org whose plan lapsed: no cost feature, hosted learning off.
      org_acme: { account: { ...pro, org_id: "org_acme", plan: "team", status: "past_due", features: ["team"], seats: 12, role: "member", learning: false, learning_note: null, orgs: listed }, costEntitled: false },
      org_sam: { account: pro, costEntitled: true },
    },
    cost: { days: 30, total: { dollars: 12.5, tokens: 1_200_000, unknown: 0, free: 200_000 }, by_model: { sonnet: { dollars: 12.5, tokens: 1_000_000, unknown: 0, free: 0 }, "qwen3-coder:30b": { dollars: 0, tokens: 200_000, unknown: 0, free: 200_000 } } },
  });
  const store = createAppStore();
  const client = daemon.client();
  const sync = startSync({ client, store, connect: daemon.connect });
  const openUrl = vi.fn((_url: string) => Promise.resolve());
  const onNavigate = vi.fn();
  render(
    <StoreContext.Provider value={store}>
      <SessionsProvider client={client}>
        <AccountScreen client={client} openUrl={openUrl} onNavigate={onNavigate} />
      </SessionsProvider>
    </StoreContext.Provider>,
  );
  return { daemon, openUrl, onNavigate, stop: () => sync.stop() };
}

const posts = (daemon: FakeDaemon, path: string) => daemon.requests.filter((r) => r.method === "POST" && r.path === path);

describe("account screen", () => {
  it("signs in through the system browser, shows the plan and what it unlocks, the spend the engine allows, switches org and signs out", async () => {
    const { daemon, openUrl, onNavigate, stop } = setup();
    // Signed out: the free tier's features locked, cost reports locked by the engine, the plans a click away.
    await screen.findByTestId("sign-in");
    expect(screen.getByText("Sign in to see your plan and what brindle is spending; brindle works without one.")).toBeTruthy();
    // The plans as the pricing page has them: the free one in a sentence, the four cards as columns
    // over their lists with Free as the person's column, and the site's own upgrade.
    const features = screen.getByTestId("features");
    expect(screen.getByTestId("features-summary").textContent).toContain("Developer Core, $0 forever: all of brindle.");
    const plans = screen.getByTestId("plans");
    expect([...plans.querySelectorAll("th[data-plan]")].map((th) => th.getAttribute("data-plan"))).toEqual(["free", "pro", "team", "enterprise"]);
    expect(plans.querySelector("th[data-current]")!.getAttribute("data-plan")).toBe("free");
    expect(plans.querySelector('th[data-plan="team"]')!.textContent).toContain("$40 / seat / month");
    expect(plans.querySelectorAll("tbody")).toHaveLength(4);
    expect(plans.querySelector('tbody[data-group="pro"]')!.textContent).toContain("Everything in Developer Core, plus");
    expect(plans.querySelector('tbody[data-group="pro"]')!.textContent).toContain("Per-worktree databases");
    expect(plans.querySelector('tbody[data-group="enterprise"]')!.textContent).toContain("Air-gap mode");
    expect(features.textContent).not.toMatch(/brindle (account|cost|rules|learning) /);
    fireEvent.click(screen.getByTestId("upgrade"));
    expect(openUrl).toHaveBeenLastCalledWith(ACCOUNT_URL);
    fireEvent.click(screen.getByTestId("pricing"));
    expect(openUrl).toHaveBeenLastCalledWith(PRICING_URL);
    await screen.findByTestId("spend-locked");
    expect(screen.getByTestId("spend-locked").textContent).toBe("🔒 Cost reports come with Pro.See the plans ↗");
    fireEvent.click(screen.getByTestId("spend-pricing"));
    expect(openUrl).toHaveBeenLastCalledWith(PRICING_URL);

    // The browser sign-in: the daemon's authorize URL opens outside the app; the page waits, then follows.
    daemon.deferLogin = true;
    fireEvent.click(screen.getByRole("button", { name: "Sign in with your browser" }));
    await waitFor(() => expect(openUrl).toHaveBeenCalledWith(daemon.data.loginUrl));
    expect(posts(daemon, "/account/login")[0].body).toEqual({ device: false });
    const state = screen.getByTestId("sign-in-state");
    expect(state.textContent).toContain("Finish signing in in your browser");
    expect(state.textContent).toContain(daemon.data.loginUrl);
    expect(openUrl).toHaveBeenCalledTimes(4);
    daemon.data.costEntitled = true;
    daemon.completeLogin();
    const plan = await screen.findByTestId("plan");
    expect(plan.textContent).toContain("sam@example.com");
    expect(screen.getByTestId("plan-pill").textContent).toBe("brindle Pro");
    expect(daemon.requests.filter((r) => r.path === "/account").at(-1)?.query).toEqual({ repo: REPO });
    fireEvent.click(within(plan).getByTestId("manage-billing"));
    expect(openUrl).toHaveBeenLastCalledWith("https://pawdelta.com/brindle/account");
    // The entitlement as the engine holds it: org and role, status and seats, renewal, hosted learning on with the learner's note.
    const entitlement = screen.getByTestId("entitlement");
    expect(entitlement.textContent).toContain("org_sam (owner)");
    expect(entitlement.textContent).toContain("active · 1 seat");
    expect(entitlement.textContent).toContain("renews by 2027-01-15");
    expect(screen.getByTestId("learning").textContent).toBe("onnot enough similar tasks yet: kept the default");
    // A paying customer: the header names the plan, the plan card is the pricing page's, and nothing is sold:
    // no comparison, no pricing link, no banner; just one quiet link up a tier in the plan card's header.
    expect(screen.getByText("Your brindle Pro plan: what it includes, and what brindle is spending.")).toBeTruthy();
    expect(screen.getByTestId("features").textContent).toContain("Power-Dev Pro");
    expect(screen.getByTestId("features-summary").textContent).toContain("Everything in Developer Core, plus");
    expect(screen.getByTestId("plan-features").textContent).toContain("Hosted learning");
    expect(screen.getByTestId("plan-features").textContent).not.toContain("picks");
    expect(screen.getByTestId("plan-features").querySelectorAll('li[data-available="false"]')).toHaveLength(0);
    expect(screen.queryByTestId("unavailable-note")).toBeNull();
    expect(screen.queryByTestId("plans")).toBeNull();
    expect(screen.queryByTestId("upgrade")).toBeNull();
    expect(screen.queryByTestId("pricing")).toBeNull();
    // The plan card: no price, one quiet link up a tier and one to the plan's guide, and each feature a link to where it is used.
    const card = screen.getByTestId("features");
    expect(card.textContent).not.toContain("/ month");
    expect(within(plan).queryByTestId("upgrade-next")).toBeNull();
    expect(within(card).getByTestId("upgrade-next").textContent).toBe("Upgrade to Team ↗");
    fireEvent.click(within(card).getByTestId("upgrade-next"));
    expect(openUrl).toHaveBeenLastCalledWith(ACCOUNT_URL);
    fireEvent.click(within(card).getByTestId("plan-guide"));
    expect(openUrl).toHaveBeenLastCalledWith(`${DOCS_URL}recommended-use/`);
    fireEvent.click(within(card).getByRole("button", { name: "Hosted learning" }));
    expect(onNavigate).toHaveBeenLastCalledWith("agents");
    fireEvent.click(within(card).getByRole("button", { name: "Settings sync" }));
    expect(onNavigate).toHaveBeenLastCalledWith("settings");
    fireEvent.click(within(card).getByRole("button", { name: /brindle CI on 1 repo/ }));
    expect(openUrl).toHaveBeenLastCalledWith(`${DOCS_URL}commands/#ci`);
    fireEvent.click(screen.getByRole("button", { name: /See everything your plan includes/ }));
    expect(screen.getByTestId("features").querySelector('[data-tier="free"]')!.textContent).toContain("Parallel agents and verified goals; brindle drives toward the goal");
    await waitFor(() => expect(screen.getByTestId("spend-total").textContent).toContain("$12.50"));
    expect(screen.getByTestId("spend").querySelector('tr[data-model="sonnet"]')!.textContent).toContain("1.0M");
    expect(daemon.requests.filter((r) => r.path === "/cost").at(-1)?.query).toEqual({ repo: REPO, days: "30" });
    fireEvent.click(screen.getByRole("button", { name: "7 days" }));
    await waitFor(() => expect(daemon.requests.filter((r) => r.path === "/cost").at(-1)?.query).toEqual({ repo: REPO, days: "7" }));

    // Switching org takes the org id (typed, while the engine lists no orgs); the engine's words come back, and what
    // it allows is asked again: this org has no cost feature, so the spend locks, and hosted learning reads off.
    const form = screen.getByRole("form", { name: "Switch org" });
    expect(form.textContent).toContain("couldn't list your orgs");
    fireEvent.change(within(form).getByLabelText("Org id"), { target: { value: "org_acme" } });
    fireEvent.click(within(form).getByRole("button", { name: "Switch org" }));
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("Switched to org org_acme."));
    expect(daemon.orgSwitches).toEqual(["org_acme"]);
    await screen.findByTestId("spend-locked");
    await waitFor(() => expect(screen.getByTestId("plan-pill").textContent).toBe("brindle Team"));
    expect(screen.getByTestId("entitlement").textContent).toContain("past_due · 12 seats");
    expect(screen.getByTestId("learning").textContent).toBe("off");
    // Still a paying customer: the engine's refusal stands alone, with nothing to buy beside it.
    // The engine's refusal, without the command it ends with.
    expect(screen.getByTestId("spend-locked").textContent).toBe("🔒 The engine refuses the cost report for this plan: cost reports need brindle Pro.");
    expect(screen.queryByTestId("spend-pricing")).toBeNull();
    expect(screen.queryByTestId("upgrade")).toBeNull();
    expect(screen.getByTestId("features").textContent).toContain("DevOps Automation");
    expect(screen.getByText("Your brindle Team plan: what it includes, and what brindle is spending.")).toBeTruthy();
    // Team's quiet link goes up to Enterprise, on the enterprise page.
    expect(within(screen.getByTestId("features")).getByTestId("upgrade-next").textContent).toBe("Upgrade to Enterprise ↗");
    fireEvent.click(within(screen.getByTestId("features")).getByTestId("upgrade-next"));
    expect(openUrl).toHaveBeenLastCalledWith(ENTERPRISE_URL);
    // The engine lists this account's orgs now: a picker, the current one marked, instead of a typed id.
    const picker = within(form).getByTestId("org-picker") as HTMLSelectElement;
    expect([...picker.options].map((o) => o.textContent)).toEqual(["Sam · personal · owner", "Acme · team · member (current)"]);
    expect(picker.value).toBe("org_acme");
    expect(within(form).getByRole("button", { name: "Switch org" })).toHaveProperty("disabled", true);
    // Its entitlement has "team" only: the plan's other lines read unavailable, quietly, and so do Pro's below.
    const lines = screen.getByTestId("plan-features");
    expect(lines.querySelector('li[data-feature="team"]')!.getAttribute("data-available")).toBe("true");
    expect(lines.querySelector('li[data-feature="ci"]')!.getAttribute("data-available")).toBe("false");
    expect(lines.querySelector('li[data-feature="ci"]')!.textContent).toContain("not in your entitlement");
    expect(lines.querySelector('li[data-feature="org_budgets"]')!.getAttribute("data-available")).toBe("false");
    expect(screen.getByTestId("unavailable-note").textContent).toContain("aren't in your entitlement right now");
    // The tiers below are still open from before: Pro's lines read unavailable too, the free ones never.
    expect(screen.getByRole("button", { name: "Hide the rest" })).toBeTruthy();
    expect(screen.getByTestId("features").querySelector('[data-tier="pro"] li[data-feature="learning"]')!.getAttribute("data-available")).toBe("false");
    expect(screen.getByTestId("features").querySelector('[data-tier="free"] li[data-feature="core"]')!.getAttribute("data-available")).toBe("true");
    // A line the entitlement lacks is no link; one it has goes where it is used (Agents for the org policy).
    expect(within(lines).queryByRole("button", { name: /brindle CI on every repo/ })).toBeNull();
    fireEvent.click(within(lines).getByRole("button", { name: "Org policy and required review" }));
    expect(onNavigate).toHaveBeenLastCalledWith("agents");
    expect(screen.getByTestId("features").textContent).not.toMatch(/Pricing|\/ seat/);
    // And back, from the picker: the report is fetched afresh and shows again.
    fireEvent.change(picker, { target: { value: "org_sam" } });
    fireEvent.click(within(form).getByRole("button", { name: "Switch org" }));
    await waitFor(() => expect(screen.getByTestId("spend-total").textContent).toContain("$12.50"));
    expect(daemon.orgSwitches).toEqual(["org_acme", "org_sam"]);
    // An org the person isn't in is refused by the engine, in its words (typed again: the personal org lists none).
    fireEvent.change(within(form).getByLabelText("Org id"), { target: { value: "org_nope" } });
    fireEvent.click(within(form).getByRole("button", { name: "Switch org" }));
    await waitFor(() => expect(within(plan).getByRole("alert").textContent).toContain("not a member of org_nope"));

    // Signing out brings the sign-in back, with the device code as the other way in.
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await screen.findByTestId("sign-in");
    expect(posts(daemon, "/account/logout")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Use a device code instead" }));
    await waitFor(() => expect(screen.getByTestId("sign-in-state").textContent).toContain("enter the code ABCD-EFGH"));
    expect(posts(daemon, "/account/login").at(-1)?.body).toEqual({ device: true });
    expect(openUrl.mock.calls.map((c) => c[0])).toEqual([ACCOUNT_URL, PRICING_URL, PRICING_URL, daemon.data.loginUrl, ACCOUNT_URL, ACCOUNT_URL, `${DOCS_URL}recommended-use/`, `${DOCS_URL}commands/#ci`, ENTERPRISE_URL]);
    stop();
  });

  it("offers Enterprise nothing to buy, not even the quiet link", async () => {
    const daemon = new FakeDaemon({
      repos: [{ repo_root: REPO, name: "proj" }],
      account: { signed_in: true, name: "cto@sovereign.example", plan: "enterprise", org_id: "org_sov", status: "active", features: ["team", "airgap", "audit"], seats: 400, role: "admin" },
    });
    const store = createAppStore();
    const client = daemon.client();
    const sync = startSync({ client, store, connect: daemon.connect });
    render(
      <StoreContext.Provider value={store}>
        <SessionsProvider client={client}>
          <AccountScreen client={client} openUrl={vi.fn(() => Promise.resolve())} />
        </SessionsProvider>
      </StoreContext.Provider>,
    );
    const plan = await screen.findByTestId("plan");
    expect(screen.getByTestId("plan-pill").textContent).toBe("brindle Enterprise");
    expect(plan.textContent).not.toMatch(/Upgrade/);
    expect(screen.queryByTestId("upgrade-next")).toBeNull();
    expect(screen.getByTestId("features").textContent).not.toMatch(/Upgrade|Pricing|\/ seat/);
    expect(within(screen.getByTestId("features")).getByTestId("plan-guide").textContent).toBe("Get the most from Enterprise ↗");
    sync.stop();
  });
});
