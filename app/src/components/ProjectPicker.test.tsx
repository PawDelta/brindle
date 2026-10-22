import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Repo } from "../api/client";
import { createAppStore } from "../store/store";
import { startSync, type Sync } from "../store/sync";
import { StoreContext } from "../store/useStore";
import { FakeDaemon } from "../test/fakeDaemon";
import { DEFAULT_PARENT, ProjectPicker } from "./ProjectPicker";

const URL = "https://github.com/sam/proj.git";
let sync: Sync | null = null;
afterEach(() => {
  sync?.stop();
  sync = null;
  cleanup();
});

/* `pickFolder` null: a browser tab, with no folder picker. */
function setup(daemon = new FakeDaemon(), pickFolder: ((title: string) => Promise<string | null>) | null = vi.fn(() => Promise.resolve("/home/u/Code"))) {
  const store = createAppStore();
  const client = daemon.client();
  sync = startSync({ client, store, connect: daemon.connect });
  const picked = vi.fn<(repo: Repo) => void>();
  render(
    <StoreContext.Provider value={store}>
      <ProjectPicker client={client} pickFolder={pickFolder ?? undefined} onPicked={picked} />
    </StoreContext.Provider>,
  );
  return { daemon, picked, pickFolder };
}

const clone = (url = URL) => {
  fireEvent.change(screen.getByRole("textbox", { name: "Repository URL" }), { target: { value: url } });
  fireEvent.click(screen.getByRole("button", { name: "Clone" }));
};

describe("ProjectPicker", () => {
  it("onboarding's Pick a project clones a repository into ~/Projects, with git's lines in the progress card", async () => {
    const { daemon, picked } = setup();
    expect(screen.getByTestId("clone-parent").textContent).toBe(DEFAULT_PARENT);
    clone();
    await waitFor(() => expect(picked).toHaveBeenCalledWith({ repo_root: "~/Projects/proj", name: "proj" }));
    expect(daemon.clones).toEqual([{ url: URL, parent_dir: "~/Projects" }]);
    const card = screen.getByTestId("clone");
    expect(card.getAttribute("data-state")).toBe("done");
    expect(within(card).getByTestId("clone-lines").textContent).toContain("Resolving deltas: 100% (611/611), done.");
  });

  it("onboarding's Pick a project shows git's own words when the clone's auth fails, and Retry clones again inline", async () => {
    const daemon = new FakeDaemon();
    daemon.cloneFails[URL] = 1;
    const { picked, pickFolder } = setup(daemon);
    // The folder to clone into, changed through the picker.
    fireEvent.click(screen.getByRole("button", { name: "Change…" }));
    await waitFor(() => expect(screen.getByTestId("clone-parent").textContent).toBe("/home/u/Code"));
    expect(pickFolder).toHaveBeenCalledWith("Clone into");
    clone();
    const failed = await screen.findByTestId("clone-failed");
    expect(failed.textContent).toContain("Clone failed.");
    expect(failed.textContent).toContain(`fatal: Authentication failed for '${URL}/'`);
    expect(picked).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(within(failed).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(picked).toHaveBeenCalledWith({ repo_root: "/home/u/Code/proj", name: "proj" }));
    expect(daemon.clones).toEqual([
      { url: URL, parent_dir: "/home/u/Code" },
      { url: URL, parent_dir: "/home/u/Code" },
    ]);
    expect(screen.queryByTestId("clone-failed")).toBeNull();
  });

  it("onboarding's Pick a project opens a folder picked with the native picker, and says why one isn't a repo", async () => {
    const daemon = new FakeDaemon();
    daemon.notGitFolders = ["/home/u/Downloads"];
    const answers = ["/home/u/Downloads", "/home/u/Projects/proj"];
    const { picked, pickFolder } = setup(daemon, vi.fn(() => Promise.resolve(answers.shift() ?? null)));
    fireEvent.click(screen.getByRole("button", { name: "Open a folder…" }));
    expect((await screen.findByRole("alert")).textContent).toContain("/home/u/Downloads is not a git repository");
    expect(picked).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Open a folder…" }));
    await waitFor(() => expect(picked).toHaveBeenCalledWith({ repo_root: "/home/u/Projects/proj", name: "proj" }));
    expect(pickFolder).toHaveBeenCalledWith("Open a folder");
    expect(daemon.opened).toEqual(["/home/u/Projects/proj"]);
  });

  it("onboarding's Pick a project without the app's folder picker still clones", () => {
    setup(new FakeDaemon(), null);
    expect((screen.getByRole("button", { name: "Open a folder…" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "Change…" })).toBeNull();
    expect((screen.getByRole("textbox", { name: "Repository URL" }) as HTMLInputElement).disabled).toBe(false);
  });
});
