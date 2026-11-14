import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AccountInfo, TrackerIntegrations } from "../../api/client";
import { Integrations } from "./Integrations";

afterEach(cleanup);

const state: TrackerIntegrations = {
  org_id: "o1",
  settings: { repos: [], allow: { create_items: true, link_back: true, close_on_merge: false, include_text: false }, who_can_create: "authors_and_reviewers" },
  trackers: {
    azure_devops: { tracker: "azure_devops", label: "Azure DevOps", configured: true, connected: true, site: "dev.azure.com/pawdelta", connected_by: "sam" },
    jira: { tracker: "jira", label: "Jira", configured: true, connected: false },
  },
  connect_url: "https://pawdelta.test/brindle/account/integrations?org_id=o1",
  repos: [{ repo: "pawdelta", repo_key: "a".repeat(64), repo_root: "/src/pawdelta" }],
};

function setup(role: string | null) {
  const client = {
    account: vi.fn().mockResolvedValue({ signed_in: true, role } as AccountInfo),
    trackerIntegrations: vi.fn().mockResolvedValue(state),
    saveTrackerSettings: vi.fn().mockResolvedValue(state),
    testTracker: vi.fn().mockResolvedValue({ ok: true }),
    disconnectTracker: vi.fn().mockResolvedValue({}),
  };
  const open = vi.fn().mockResolvedValue(undefined);
  render(<Integrations client={client} open={open} />);
  return { client, open };
}

describe("Settings › Integrations", () => {
  it("shows nothing to a member", async () => {
    const { client } = setup("member");
    await waitFor(() => expect(client.account).toHaveBeenCalled());
    expect(client.trackerIntegrations).not.toHaveBeenCalled();
    expect(screen.queryByTestId("integrations")).toBeNull();
  });

  it("lets an admin connect in the browser, map a repo, switch what brindle may do, and save it", async () => {
    const { client, open } = setup("admin");
    expect(await screen.findByText(/dev.azure.com\/pawdelta/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Connect Jira…" }));
    expect(open).toHaveBeenCalledWith(state.connect_url);
    fireEvent.change(screen.getByLabelText("pawdelta tracker"), { target: { value: "azure_devops" } });
    fireEvent.change(screen.getByLabelText("pawdelta project"), { target: { value: "PawDelta" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /Close a story when its task's branch merges/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(client.saveTrackerSettings).toHaveBeenCalled());
    const sent = client.saveTrackerSettings.mock.calls[0][0];
    expect(sent.repos).toEqual([{ repo: "pawdelta", repo_key: "a".repeat(64), tracker: "azure_devops", project: "PawDelta" }]);
    expect(sent.allow.close_on_merge).toBe(true);
    expect(sent.types.azure_devops.task).toBe("User Story");
    expect(sent.types.jira).toBeUndefined();
  });

  it("tests and disconnects a connected tracker", async () => {
    const { client } = setup("owner");
    await screen.findByText(/dev.azure.com\/pawdelta/);
    fireEvent.click(screen.getByRole("button", { name: "Test" }));
    await waitFor(() => expect(client.testTracker).toHaveBeenCalledWith("azure_devops"));
    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(client.disconnectTracker).toHaveBeenCalledWith("azure_devops"));
  });
});
