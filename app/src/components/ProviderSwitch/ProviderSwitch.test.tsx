import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { InboxItemDTO } from "../../api/schema";
import { DaemonClient } from "../../api/client";
import { BASE_URL, FakeDaemon } from "../../test/fakeDaemon";
import { inTestContext } from "../../test/harness";
import { createAppStore } from "../../store/store";
import { SettingsScreen } from "../../screens/Settings";
import { ProviderSwitchCard, answerProviderSwitch, offerOf } from "./ProviderSwitch";

const { tauri, shellFetch } = vi.hoisted(() => ({ tauri: vi.fn(() => false), shellFetch: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ isTauri: tauri }));
vi.mock("../../api/tauri", () => ({ tauriFetch: (...a: unknown[]) => shellFetch(...a) }));
vi.mock("../../components/PtyTerminal", () => import("../../test/panes"));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  tauri.mockReturnValue(false);
});

const item = (kind: string, detail: string): InboxItemDTO => ({
  kind,
  reason: "",
  detail,
  request_id: "offer-1",
  root_id: "root-1",
  action: { method: "POST", path: "/v1/sessions/root-1/provider-switch" },
});

/* A local fake daemon: records requests, answers when told to. */
function setup(it_: InboxItemDTO) {
  let answer = () => {};
  const fetchImpl = vi.fn(() => new Promise<Response>((resolve) => {
    answer = () => resolve(new Response("{}", { status: 200 }));
  }));
  function Inbox() {
    const [items, setItems] = useState([it_]);
    return <>{items.map((i) => (
      <ProviderSwitchCard key={i.request_id} offer={offerOf(i)}
        onAnswer={(accept) => answerProviderSwitch({ baseUrl: "http://d", token: "t" }, i, accept, fetchImpl as unknown as typeof fetch).then(() => setItems([]))} />
    ))}</>;
  }
  render(<Inbox />);
  return { fetchImpl, finish: () => act(async () => answer()) };
}

describe("answerProviderSwitch", () => {
  it("in the desktop app goes through the shell's transport, not global fetch", async () => {
    const shell = vi.fn(async () => new Response("{}", { status: 200 }));
    tauri.mockReturnValue(true);
    shellFetch.mockImplementation(shell);
    const global = vi.spyOn(globalThis, "fetch");
    await answerProviderSwitch({ baseUrl: "http://brindle-daemon.invalid", token: "" }, item("provider_switch", "claude → codex"), true);
    expect(shell).toHaveBeenCalledTimes(1);
    expect(global).not.toHaveBeenCalled();
  });
});

describe("ProviderSwitchCard", () => {
  it("always shows the post-switch notice with switch-back and dismiss actions", async () => {
    function Notice() {
      const [visible, setVisible] = useState(true);
      const [provider, setProvider] = useState("codex");
      return <>
        <output aria-label="Current provider">{provider}</output>
        {visible && <ProviderSwitchCard variant="notice"
          offer={{ ...offerOf(item("provider_switch", "claude → codex")), limitedUntil: new Date(2026, 10, 16, 9).getTime() / 1000 }}
          onAnswer={async (accept) => { if (accept) setProvider("claude"); else setVisible(false); }} />}
      </>;
    }
    render(<Notice />);
    expect(screen.getByText("Switched to Codex · Claude was out of quota, back at 9:00am")).toBeTruthy();
    expect(screen.queryByRole("checkbox")).toBeNull();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Switch back now" })));
    expect(screen.getByLabelText("Current provider").textContent).toBe("claude");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Dismiss" })));
    expect(screen.queryByTestId("provider-switch")).toBeNull();
  });

  it("always switches automatically and reads the saved preference in Settings", async () => {
    const daemon = new FakeDaemon();
    let quotaSwitch = "ask";
    const switches: Record<string, unknown>[] = [];
    // Extend the settings tests' daemon locally for the new setting and route.
    const fetchImpl: typeof fetch = async (input, init) => {
      const path = new URL(String(input)).pathname;
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : {};
      if (path.endsWith("/provider-switch")) {
        switches.push(body);
        if (body.accept && body.always) quotaSwitch = "auto";
        return Response.json({});
      }
      if (path.endsWith("/user-settings")) {
        if (init?.method === "PATCH" && body.quota_switch) quotaSwitch = body.quota_switch;
        return Response.json({ ...daemon.data.userSettings, quota_switch: quotaSwitch });
      }
      return daemon.fetch(input, init);
    };
    const client = new DaemonClient({ baseUrl: BASE_URL, token: daemon.token, fetch: fetchImpl });
    const offer = item("provider_switch", "claude → codex");
    const view = render(<ProviderSwitchCard offer={offerOf(offer)}
      onAnswer={(accept, always) => answerProviderSwitch(client, offer, accept, fetchImpl, always)} />);
    fireEvent.click(screen.getByRole("checkbox", { name: "Always switch automatically" }));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Switch" })));
    expect(switches).toEqual([{ offer: "offer-1", accept: true, always: true }]);
    view.unmount();

    const settings = () => render(inTestContext(<SettingsScreen client={client} />, { client, store: createAppStore() },
      { daemon: true, sessions: true, providers: true }));
    const first = settings();
    const select = await screen.findByRole("combobox", { name: "When quota runs out" });
    await waitFor(() => expect((select as HTMLSelectElement).value).toBe("auto"));
    expect(within(select).getByRole("option", { selected: true }).textContent).toBe("Switch automatically");
    fireEvent.change(select, { target: { value: "ask" } });
    await waitFor(() => expect(quotaSwitch).toBe("ask"));
    await waitFor(() => expect((select as HTMLSelectElement).disabled).toBe(false));
    first.unmount();
    settings();
    await waitFor(() => expect((screen.getByRole("combobox", { name: "When quota runs out" }) as HTMLSelectElement).disabled).toBe(false));
    expect((screen.getByRole("combobox", { name: "When quota runs out" }) as HTMLSelectElement).value).toBe("ask");
  });

  it("a double-pressed Switch sends one request and the card leaves once handled", async () => {
    const { fetchImpl, finish } = setup(item("provider_switch", "claude → codex"));
    expect(screen.getByTestId("provider-switch").dataset.kind).toBe("switch");
    const button = screen.getByRole("button", { name: "Switch" });
    fireEvent.click(button);
    fireEvent.click(button);
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://d/v1/sessions/root-1/provider-switch");
    expect(JSON.parse(init.body as string)).toEqual({ offer: "offer-1", accept: true });
    await finish();
    expect(screen.queryByTestId("provider-switch")).toBeNull();
  });

  it("one card for every session on a limit: one Switch is one request, and its sessions are read", async () => {
    const { fetchImpl, finish } = setup(item("provider_switch", "claude → codex · 3 sessions: a, b, c"));
    const card = screen.getByTestId("provider-switch");
    expect(card.dataset.sessions).toBe("3");
    fireEvent.click(within(card).getByRole("button", { name: "Switch" }));
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(JSON.parse((fetchImpl.mock.calls[0] as unknown as [string, RequestInit])[1].body as string).offer).toBe("offer-1");
    await finish();
    const notice = offerOf(item("provider_switch_back", "notice: claude → codex · 03:00 · 2 sessions: x, y"));
    expect([notice.fromProvider, notice.toProvider, notice.resetAt, notice.sessions]).toEqual(["claude", "codex", "03:00", ["x", "y"]]);
  });

  it("Not now declines, and the switch-back offer names the provider with quota again", async () => {
    const { fetchImpl, finish } = setup(item("provider_switch_back", "codex → claude"));
    expect(screen.getByTestId("provider-switch").dataset.kind).toBe("back");
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    expect(JSON.parse((fetchImpl.mock.calls[0] as unknown as [string, RequestInit])[1].body as string).accept).toBe(false);
    await finish();
    expect(screen.queryByTestId("provider-switch")).toBeNull();
  });
});
