import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AccountInfo, TrackerIntegrations, TrackerName, TrackerSettings } from "../../api/client";
import { Integrations } from "./Integrations";

afterEach(cleanup);

const KEY = "a".repeat(64);

/* The daemon, as far as Settings can tell: what is saved and connected is kept, and read back. */
function server(role: string | null, org = "o1") {
  const connected: Record<TrackerName, boolean> = { azure_devops: true, jira: false };
  let settings: Partial<TrackerSettings> = { repos: [] };
  const view = (): TrackerIntegrations => ({
    org_id: org,
    settings,
    trackers: {
      azure_devops: { tracker: "azure_devops", label: "Azure DevOps", configured: true, connected: connected.azure_devops, site: "dev.azure.com/pawdelta" },
      jira: { tracker: "jira", label: "Jira", configured: true, connected: connected.jira },
    },
    connect_url: "https://pawdelta.test/brindle/account/integrations?org_id=o1",
    repos: [{ repo: "pawdelta", repo_key: KEY, repo_root: "/src/pawdelta" }],
  });
  const active = { org };
  return {
    active,
    client: {
      account: vi.fn().mockResolvedValue({ signed_in: true, role } as AccountInfo),
      trackerIntegrations: vi.fn(async () => view()),
      saveTrackerSettings: vi.fn(async (next: Partial<TrackerSettings>, loadedFor: string) => {
        if (loadedFor !== active.org) throw new Error("org changed");
        settings = next;
        return view();
      }),
      testTracker: vi.fn(async (_t: TrackerName, loadedFor: string) => {
        if (loadedFor !== active.org) throw new Error("org changed");
        return { tracker: _t, ok: true };
      }),
      disconnectTracker: vi.fn(async (t: TrackerName, loadedFor: string) => {
        if (loadedFor !== active.org) throw new Error("org changed");
        connected[t] = false;
        return {};
      }),
    },
  };
}

function setup(role: string | null) {
  const s = server(role);
  const open = vi.fn().mockResolvedValue(undefined);
  render(<Integrations client={s.client} open={open} />);
  return { ...s, open };
}

const row = (tracker: string) => document.querySelector(`[data-tracker="${tracker}"]`)!;

describe("Settings › Integrations", () => {
  it("shows nothing to a member", async () => {
    const { client } = setup("member");
    await waitFor(() => expect(client.account).toHaveBeenCalled());
    expect(client.trackerIntegrations).not.toHaveBeenCalled();
    expect(screen.queryByTestId("integrations")).toBeNull();
  });

  it("keeps what an admin saves, done states typed one at a time included, across a reload", async () => {
    const { open } = setup("admin");
    await screen.findByLabelText("Azure DevOps done states");
    fireEvent.click(screen.getByRole("button", { name: /^Connect Jira/ }));
    expect(open).toHaveBeenCalledWith(expect.stringContaining("/brindle/account/integrations"));
    fireEvent.change(screen.getByLabelText("pawdelta tracker"), { target: { value: "azure_devops" } });
    fireEvent.change(screen.getByLabelText("pawdelta project"), { target: { value: "PawDelta" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /Close a story when its task's branch merges/ }));
    const done = screen.getByLabelText("Azure DevOps done states") as HTMLInputElement;
    for (const typed of ["Resolved", "Resolved,", "Resolved, ", "Resolved, Closed", "Resolved, Closed for good"]) fireEvent.change(done, { target: { value: typed } });
    expect(done.value).toBe("Resolved, Closed for good");     // nothing was dropped while typing
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect((screen.getByLabelText("Azure DevOps done states") as HTMLInputElement).value).toBe("Resolved, Closed for good"));
    expect((screen.getByLabelText("pawdelta project") as HTMLInputElement).value).toBe("PawDelta");
    expect((screen.getByRole("checkbox", { name: /Close a story when its task's branch merges/ }) as HTMLInputElement).checked).toBe(true);
    await waitFor(() => expect(screen.getByRole("status")).toBeTruthy());
  });

  it("disconnecting turns the tracker off in the page, and a stale page can't act on another org", async () => {
    const { client, active } = setup("owner");
    await screen.findByLabelText("Azure DevOps done states");
    fireEvent.click(screen.getByRole("button", { name: "Test" }));
    await waitFor(() => expect(screen.getByRole("status")).toBeTruthy());
    active.org = "o2";                                       // another window switched organization
    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    expect((await screen.findByRole("alert"))).toBeTruthy();
    expect(row("azure_devops").getAttribute("data-connected")).toBe("true");
    active.org = "o1";
    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(row("azure_devops").getAttribute("data-connected")).toBe("false"));
    expect(client.disconnectTracker).toHaveBeenLastCalledWith("azure_devops", "o1");
  });
});
