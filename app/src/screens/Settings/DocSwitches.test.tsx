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
    const client = setup(vi.fn().mockResolvedValue([SWITCHES[0], { ...SWITCHES[1], on: false, choice: "off" }]));
    const store = (await screen.findByRole("switch", { name: "Store docs" })) as HTMLButtonElement;
    expect(store.disabled).toBe(true);
    expect(screen.getByText("Set by your organisation: off.")).toBeTruthy();
    fireEvent.click(screen.getByRole("switch", { name: "Sharing" }));
    await waitFor(() => expect(screen.getByRole("switch", { name: "Sharing" }).getAttribute("aria-checked")).toBe("false"));
    expect(client.setDocSwitch).toHaveBeenCalledWith("share", false);
  });

  it("shows the daemon's refusal", async () => {
    setup(vi.fn().mockRejectedValue(new Error("your organisation has share off for everyone")));
    fireEvent.click(await screen.findByRole("switch", { name: "Sharing" }));
    expect((await screen.findByRole("alert")).textContent).toContain("share off for everyone");
    expect(screen.getByRole("switch", { name: "Sharing" }).getAttribute("aria-checked")).toBe("true");
  });
});
