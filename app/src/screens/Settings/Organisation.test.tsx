import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DaemonClient, type OrgSettings } from "../../api/client";
import { Organisation } from "./Organisation";
import { SettingsRail } from "../../components/SettingsRail";
import { createAppStore } from "../../store/store";
import { StoreContext } from "../../store/useStore";

afterEach(cleanup);

it("reads the admin rows, edits one at a time and saves an enforced value", async () => {
  let state: OrgSettings = { org_id: "org_test", actor_sub: "u_admin", can_edit_org: true, can_edit_groups: true, policy: {
    switches: { share: "enforced_on", prompt_analysis: "enforced_off", store: "off", assist: "default_on", import_upload: "off" },
    groups: {}, share_links: "org",
  } };
  const client = new DaemonClient({ baseUrl: "http://daemon", token: "t", fetch: vi.fn(async (_url, init) => {
    if (init?.method === "PUT") {
      const body = JSON.parse(String(init.body));
      state = { ...state, policy: { ...state.policy, switches: { ...state.policy.switches, [body.name]: body.value } } };
    }
    return Response.json(state);
  }) as typeof fetch });
  const store = createAppStore();
  store.dispatch({ type: "account", account: { signed_in: true, role: "admin" } });
  render(<StoreContext.Provider value={store}><SettingsRail tab="organisation" onTab={() => undefined} onBack={() => undefined} /><Organisation client={client} /></StoreContext.Provider>);
  expect(screen.getByRole("tab", { name: "Organisation" })).toBeTruthy();
  expect(await screen.findByTestId("org-setting-share")).toBeTruthy();
  expect(screen.getAllByRole("button", { name: /^Change / })).toHaveLength(6);
  fireEvent.click(screen.getByRole("button", { name: "Change Where plans are kept" }));
  expect(screen.getByTestId("org-setting-store").getAttribute("data-state")).toBe("editing");
  fireEvent.click(screen.getByRole("button", { name: "Change Prompt analysis" }));
  expect(screen.getByTestId("org-setting-store").getAttribute("data-state")).toBe("reading");
  expect(screen.getAllByRole("group")).toHaveLength(1);
  fireEvent.click(screen.getByRole("radio", { name: /^On/ }));
  fireEvent.click(screen.getByRole("button", { name: "Save for everyone" }));
  await waitFor(() => expect(screen.getByTestId("org-setting-prompt_analysis").getAttribute("data-state")).toBe("reading"));
  expect(state.policy.switches.prompt_analysis).toBe("enforced_on");
  expect(within(screen.getByTestId("org-setting-prompt_analysis")).queryByRole("radio")).toBeNull();
  act(() => store.dispatch({ type: "account", account: { signed_in: true, role: "member" } }));
  expect(screen.queryByRole("tab", { name: "Organisation" })).toBeNull();
});

it("keeps Team organisation rows read-only while allowing group settings", async () => {
  const state: OrgSettings = { org_id: "org_team", actor_sub: "u_admin", can_edit_org: false, can_edit_groups: true,
    policy: { switches: {}, groups: { engineering: { switches: { assist: "default_on" } } }, share_links: "org" } };
  const client = { orgSettings: vi.fn().mockResolvedValue(state), setOrgSetting: vi.fn(async (_state, name, value, group) => {
    state.policy.groups[group].switches[name as "assist"] = value;
    return state;
  }) };
  render(<Organisation client={client} />);
  await screen.findByTestId("org-setting-share");
  expect(screen.queryByRole("button", { name: /^Change / })).toBeNull();
  fireEvent.change(screen.getByRole("combobox", { name: "Settings for" }), { target: { value: "engineering" } });
  fireEvent.click(screen.getByRole("button", { name: "Change Briefs" }));
  fireEvent.click(screen.getByRole("radio", { name: /^Off/ }));
  fireEvent.click(screen.getByRole("button", { name: "Save for group" }));
  await waitFor(() => expect(screen.getByTestId("org-setting-assist").getAttribute("data-state")).toBe("reading"));
  expect(state.policy.groups.engineering.switches.assist).toBe("off");
});

it("shows enforced organisation values over opposing group choices", async () => {
  const state: OrgSettings = { org_id: "org_enterprise", actor_sub: "u_admin", can_edit_org: true, can_edit_groups: true,
    policy: { switches: { assist: "enforced_off", store: "enforced_on" },
      groups: { engineering: { switches: { assist: "default_on", store: "enforced_off" } } } } };
  render(<Organisation client={{ orgSettings: vi.fn().mockResolvedValue(state), setOrgSetting: vi.fn() }} />);
  fireEvent.change(await screen.findByRole("combobox", { name: "Settings for" }), { target: { value: "engineering" } });
  const briefs = within(screen.getByTestId("org-setting-assist"));
  const storage = within(screen.getByTestId("org-setting-store"));
  expect(briefs.getByText("Off", { exact: true })).toBeTruthy();
  expect(storage.getByText("brindle servers", { exact: true })).toBeTruthy();
  expect(briefs.queryByRole("button")).toBeNull();
  expect(storage.queryByRole("button")).toBeNull();
});
