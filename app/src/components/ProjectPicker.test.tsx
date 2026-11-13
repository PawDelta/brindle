import { inTestStore, trackedSync as startSync } from "../test/harness";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Repo } from "../api/client";
import { createAppStore } from "../store/store";
import { type Sync } from "../store/sync";
import { FakeDaemon } from "../test/fakeDaemon";
import { ProjectPicker } from "./ProjectPicker";

const URL = "https://github.com/sam/proj.git";
let sync: Sync | null = null;
afterEach(() => {
  sync?.stop();
  sync = null;
  cleanup();
});

/* `pickFolder` null: a browser tab, with no folder picker. */
function setup(daemon = new FakeDaemon(), pickFolder: ((title: string) => Promise<string | null>) | null = vi.fn(() => Promise.resolve("/home/u/Code")), offerGithub = false) {
  const store = createAppStore();
  const client = daemon.client();
  sync = startSync({ client, store, connect: daemon.connect });
  const picked = vi.fn<(repo: Repo) => void>();
  render(
    inTestStore(<><ProjectPicker client={client} pickFolder={pickFolder ?? undefined} onPicked={picked} offerGithub={offerGithub} openUrl={() => Promise.resolve()} /></>, store),
  );
  return { daemon, picked, pickFolder };
}

const clone = (url = URL) => {
  fireEvent.change(screen.getByRole("textbox", { name: "Repository URL" }), { target: { value: url } });
  fireEvent.click(screen.getByRole("button", { name: "Clone" }));
};

describe("ProjectPicker", () => {
  it("lets a first project clone into a new folder without a native picker", async () => {
    const { picked } = setup(new FakeDaemon(), null);
    fireEvent.change(screen.getByRole("textbox", { name: "Clone into" }), { target: { value: " " } });
    clone();
    expect(screen.getByRole("button", { name: "Clone" })).toHaveProperty("disabled", true);
    fireEvent.change(screen.getByRole("textbox", { name: "Clone into" }), { target: { value: "~/Code/new-projects" } });
    clone();
    await waitFor(() => expect(picked).toHaveBeenCalledWith({ repo_root: "~/Code/new-projects/proj", name: "proj" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("onboarding's Pick a project shows git's own words when the clone's auth fails, and Retry clones again inline", async () => {
    const daemon = new FakeDaemon();
    daemon.cloneFails[URL] = 1;
    const { picked, pickFolder } = setup(daemon);
    // The folder to clone into, changed through the picker.
    fireEvent.click(screen.getByRole("button", { name: "Change…" }));
    await waitFor(() => expect(screen.getByTestId("clone-parent")).toHaveProperty("value", "/home/u/Code"));
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

  it("offers Connect GitHub on the first launch (skippable), and a private clone that fails connects a token (a refused one is cleared and explained) and clones again", async () => {
    const daemon = new FakeDaemon();
    daemon.github.gh_available = false;
    daemon.privateRepos = [URL];
    const { picked } = setup(daemon, null, true);
    await screen.findByRole("region", { name: "Connect GitHub" });
    fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    expect(screen.queryByRole("region", { name: "Connect GitHub" })).toBeNull();

    clone();
    const failed = await screen.findByTestId("clone-failed");
    fireEvent.click(within(failed).getByRole("button", { name: "Connect GitHub" }));
    const token = await screen.findByLabelText("GitHub token");
    expect(token.getAttribute("type")).toBe("password");
    fireEvent.change(token, { target: { value: "bad-token" } });
    fireEvent.click(screen.getByRole("button", { name: "Save token" }));
    await within(screen.getByRole("region", { name: "Connect GitHub" })).findByRole("alert");
    expect((screen.getByLabelText("GitHub token") as HTMLInputElement).value).toBe("");
    expect(picked).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("GitHub token"), { target: { value: "github_pat_ok" } });
    fireEvent.click(screen.getByRole("button", { name: "Save token" }));
    await waitFor(() => expect(picked).toHaveBeenCalledWith({ repo_root: "~/Projects/proj", name: "proj" }));
    expect(daemon.githubRoutes).toEqual(["token", "token"]);
    expect(daemon.clones).toHaveLength(2);
  });

  it("a connect still in flight when the failed clone is dismissed does not clone again", async () => {
    const daemon = new FakeDaemon();
    daemon.privateRepos = [URL];
    const { picked } = setup(daemon, null);
    clone();
    const failed = await screen.findByTestId("clone-failed");
    fireEvent.click(within(failed).getByRole("button", { name: "Connect GitHub" }));
    daemon.holdGithub = true;
    fireEvent.click(await screen.findByRole("button", { name: "Use my GitHub CLI sign-in" }));
    fireEvent.click(within(failed).getByRole("button", { name: "Dismiss" }));
    await act(async () => daemon.releaseGithub());
    expect(daemon.githubRoutes).toEqual(["gh"]);
    expect(daemon.clones).toHaveLength(1);
    expect(picked).not.toHaveBeenCalled();
  });

  it("initializes a plain folder with one click after opening it", async () => {
    const daemon = new FakeDaemon();
    daemon.notGitFolders = ["/home/u/Downloads", "/home/u/New"];
    const answers = ["/home/u/Downloads", "/home/u/New"];
    const { picked, pickFolder } = setup(daemon, vi.fn(() => Promise.resolve(answers.shift() ?? null)));
    fireEvent.click(screen.getByRole("button", { name: "Open a folder…" }));
    await screen.findByRole("button", { name: "Set it up with git" });
    expect(picked).not.toHaveBeenCalled();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Pick another" })); });
    expect(picked).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Set it up with git" }));
    await waitFor(() => expect(picked).toHaveBeenCalledWith({ repo_root: "/home/u/New", name: "New" }));
    expect(pickFolder).toHaveBeenCalledWith("Open a folder");
    expect(daemon.opened).toEqual(["/home/u/New"]);
  });

  it("without the Command Line Tools, installs only on a click and Try again repeats the operation that failed last", async () => {
    const daemon = new FakeDaemon();
    daemon.toolsMissing = true;
    const { picked } = setup(daemon, vi.fn(() => Promise.resolve("/home/u/Code")));
    clone();
    await screen.findByTestId("developer-tools-missing");
    // Then a folder, which fails the same way: Try again must repeat the folder, not the older clone.
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Open a folder…" })); });
    await screen.findByTestId("developer-tools-missing");
    expect(screen.queryByTestId("clone-failed")).toBeNull();
    expect(daemon.toolsInstalls).toBe(0);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Install Command Line Tools" })); });
    expect(daemon.toolsInstalls).toBe(1);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Try again" })); });
    await waitFor(() => expect(picked).toHaveBeenCalledWith({ repo_root: "/home/u/Code", name: "Code" }));
    expect(daemon.clones).toHaveLength(1);
    expect(screen.queryByTestId("developer-tools-missing")).toBeNull();
  });
});
