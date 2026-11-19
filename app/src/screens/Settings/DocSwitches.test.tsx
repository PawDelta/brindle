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

  it("moves the library, says when some plans weren't moved, and moves them on a repeat choice", async () => {
    const free = { ...SWITCHES[0], locked: false, org: null };
    const servers = [{ ...free, on: true, choice: "on" as const }];
    const setDocSwitch = vi.fn().mockResolvedValueOnce({ switches: servers, moved: 2, left: 1 })
      .mockResolvedValueOnce({ switches: servers, moved: 1, left: 0 });
    render(<DocSwitches client={{ docSwitches: vi.fn().mockResolvedValue([free]), setDocSwitch }} />);
    fireEvent.click(await screen.findByRole("radio", { name: "brindle servers" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByRole("radio", { name: "brindle servers" }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByRole("radio", { name: "brindle servers" }));     // the advertised repeat
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(setDocSwitch.mock.calls).toEqual([["store", true], ["store", true]]);
    fireEvent.click(screen.getByRole("radio", { name: "brindle servers" }));     // all moved: nothing to do
    expect(setDocSwitch).toHaveBeenCalledTimes(2);
  });

  it("shows the daemon's refusal", async () => {
    setup(vi.fn().mockRejectedValue(new Error("your organisation has share off for everyone")));
    fireEvent.click(await screen.findByRole("switch", { name: "Sharing" }));
    expect((await screen.findByRole("alert")).textContent).toContain("share off for everyone");
    expect(screen.getByRole("switch", { name: "Sharing" }).getAttribute("aria-checked")).toBe("true");
  });
});
