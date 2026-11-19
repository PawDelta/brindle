import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DocSwitch } from "../../api/client";
import { DocSwitches } from "./DocSwitches";

afterEach(cleanup);

const SWITCHES: DocSwitch[] = [
  { name: "store", on: false, choice: null, locked: true, org: "enforced_off" },
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
    expect(mac.disabled && mac.getAttribute("aria-checked") === "true").toBe(true);
    expect(screen.getByText("Set by your organisation: This Mac.")).toBeTruthy();
    fireEvent.click(screen.getByRole("switch", { name: "Sharing" }));
    await waitFor(() => expect(screen.getByRole("switch", { name: "Sharing" }).getAttribute("aria-checked")).toBe("false"));
    expect(client.setDocSwitch).toHaveBeenCalledWith("share", false);
  });

  it("moves the library and says when some plans weren't moved", async () => {
    const free = { ...SWITCHES[0], locked: false, org: null };
    const client = { docSwitches: vi.fn().mockResolvedValue([free]),
      setDocSwitch: vi.fn().mockResolvedValue({ switches: [{ ...free, on: true, choice: "on" }], moved: 2, left: 1 }) };
    render(<DocSwitches client={client} />);
    fireEvent.click(await screen.findByRole("radio", { name: "brindle servers" }));
    expect(client.setDocSwitch).toHaveBeenCalledWith("store", true);
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByRole("radio", { name: "brindle servers" }).getAttribute("aria-checked")).toBe("true");
  });

  it("shows the daemon's refusal", async () => {
    setup(vi.fn().mockRejectedValue(new Error("your organisation has share off for everyone")));
    fireEvent.click(await screen.findByRole("switch", { name: "Sharing" }));
    expect((await screen.findByRole("alert")).textContent).toContain("share off for everyone");
    expect(screen.getByRole("switch", { name: "Sharing" }).getAttribute("aria-checked")).toBe("true");
  });
});
