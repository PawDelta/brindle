import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { InboxItemDTO } from "../../api/schema";
import { ProviderSwitchCard, answerProviderSwitch, offerOf } from "./ProviderSwitch";

const { tauri, shellFetch } = vi.hoisted(() => ({ tauri: vi.fn(() => false), shellFetch: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ isTauri: tauri }));
vi.mock("../../api/tauri", () => ({ tauriFetch: (...a: unknown[]) => shellFetch(...a) }));

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

  it("Not now declines, and the switch-back offer names the provider with quota again", async () => {
    const { fetchImpl, finish } = setup(item("provider_switch_back", "codex → claude"));
    expect(screen.getByTestId("provider-switch").dataset.kind).toBe("back");
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    expect(JSON.parse((fetchImpl.mock.calls[0] as unknown as [string, RequestInit])[1].body as string).accept).toBe(false);
    await finish();
    expect(screen.queryByTestId("provider-switch")).toBeNull();
  });
});
