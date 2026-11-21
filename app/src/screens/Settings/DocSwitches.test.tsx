import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DocSwitch } from "../../api/client";
import { DocSwitches } from "./DocSwitches";

afterEach(cleanup);

const SWITCHES: DocSwitch[] = [
  { name: "store", on: true, choice: null, locked: true, org: "enforced_on" },
  { name: "share", on: true, choice: null, locked: false, org: null },
];

function setup(setDocSwitch = vi.fn()) {
  const client = { docSwitches: vi.fn().mockResolvedValue(SWITCHES), setDocSwitch };
  render(<DocSwitches client={client} />);
  return client;
}

describe("DocSwitches", () => {
  it("sets an unlocked switch and shows a locked one with the org's value, unchangeable", async () => {
    const client = setup(vi.fn().mockResolvedValue({ switches: [SWITCHES[0], { ...SWITCHES[1], on: false, choice: "off" }] }));
    const mac = (await screen.findByRole("radio", { name: "This Mac" })) as HTMLButtonElement;
    expect(mac.disabled && mac.getAttribute("aria-checked") === "false").toBe(true);
    fireEvent.click(screen.getByRole("switch", { name: "Sharing" }));
    await waitFor(() => expect(screen.getByRole("switch", { name: "Sharing" }).getAttribute("aria-checked")).toBe("false"));
    expect(client.setDocSwitch).toHaveBeenCalledWith("share", false);
  });

  it("retries a partial library move", async () => {
    const free = { ...SWITCHES[0], on: false, locked: false, org: null };
    const servers = [{ ...free, on: true, choice: "on" as const }];
    const setDocSwitch = vi.fn().mockResolvedValueOnce({ switches: servers, moved: 2, left: 1 })
      .mockResolvedValueOnce({ switches: servers, moved: 1, left: 0 });
    render(<DocSwitches client={{ docSwitches: vi.fn().mockResolvedValue([free]), setDocSwitch }} />);
    fireEvent.click(await screen.findByRole("radio", { name: "brindle servers" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByRole("radio", { name: "brindle servers" }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(screen.getByRole("status").textContent).toContain("3 plans moved to brindle servers.");
    fireEvent.click(screen.getByRole("radio", { name: "brindle servers" }));     // all moved: nothing to do
    expect(setDocSwitch).toHaveBeenCalledTimes(2);
  });

  it("shows the moved count and enabled features, then moves back", async () => {
    const local: DocSwitch[] = [
      { ...SWITCHES[0], on: false, locked: false, org: null },
      ...(["share", "assist", "import_upload", "prompt_analysis"] as const).map(name => ({ name, on: false, choice: null, locked: false, org: null })),
    ];
    const servers = local.map(s => ({ ...s, on: true }));
    const setDocSwitch = vi.fn().mockResolvedValueOnce({ switches: servers, moved: 6, left: 0 })
      .mockResolvedValueOnce({ switches: local, moved: 6, left: 0 });
    render(<DocSwitches client={{ docSwitches: vi.fn().mockResolvedValue(local), setDocSwitch }} />);
    fireEvent.click(await screen.findByRole("radio", { name: "brindle servers" }));
    expect((await screen.findByRole("status")).textContent).toContain("6 plans moved to brindle servers. Sharing, Briefs, Imported folders and Prompt analysis are on.");
    for (const toggle of screen.getAllByRole("switch")) {
      expect(toggle.getAttribute("aria-checked")).toBe("true");
      expect((toggle as HTMLButtonElement).disabled).toBe(false);
    }
    fireEvent.click(screen.getByRole("button", { name: "Move back" }));
    await waitFor(() => expect(screen.getByRole("radio", { name: "This Mac" }).getAttribute("aria-checked")).toBe("true"));
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getAllByRole("switch").every(toggle => (toggle as HTMLButtonElement).disabled)).toBe(true);
  });

  it("disables every server-only switch on this Mac and preserves admin locks", async () => {
    const local: DocSwitch[] = [
      { ...SWITCHES[0], on: false, org: "enforced_off" },
      ...(["share", "assist", "import_upload", "prompt_analysis"] as const).map(name => ({ name, on: false, choice: null, locked: name === "assist", org: name === "assist" ? "enforced_off" as const : null })),
    ];
    const setDocSwitch = vi.fn();
    render(<DocSwitches client={{ docSwitches: vi.fn().mockResolvedValue(local), setDocSwitch }} />);
    await screen.findByRole("radio", { name: "This Mac" });
    for (const toggle of screen.getAllByRole("switch")) {
      expect((toggle as HTMLButtonElement).disabled).toBe(true);
      expect(document.getElementById(toggle.getAttribute("aria-describedby")!)?.textContent).toBeTruthy();
      fireEvent.click(toggle);
    }
    expect(setDocSwitch).not.toHaveBeenCalled();
    expect(screen.getByTestId("doc-switch-assist").getAttribute("data-state")).toBe("locked");
  });

  it("shows the daemon's refusal", async () => {
    setup(vi.fn().mockRejectedValue(new Error("your organisation has share off for everyone")));
    fireEvent.click(await screen.findByRole("switch", { name: "Sharing" }));
    expect((await screen.findByRole("alert")).textContent).toContain("share off for everyone");
    expect(screen.getByRole("switch", { name: "Sharing" }).getAttribute("aria-checked")).toBe("true");
  });
});
