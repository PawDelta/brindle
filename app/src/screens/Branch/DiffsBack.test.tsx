import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "../../App";
import { ONBOARDED_KEY } from "../../api/providers";
import { connectDaemon, renderApp } from "../../test/harness";
import { FakeDaemon } from "../../test/fakeDaemon";
import { REPO, root } from "../../test/fixtures";

vi.mock("../../components/PtyTerminal", () => import("../../test/panes"));

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem(ONBOARDED_KEY, "1");
});
afterEach(cleanup);

it("gives deep paths their room: one row per folder chain, the file's name kept whole, the unified lines as wide as their text", async () => {
  const path = "src/brindle/daemon/session_review.py";
  const daemon = new FakeDaemon({
    repos: [{ repo_root: REPO, name: "proj" }],
    sessions: [{ id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 1, goal: "Ship the change" }],
    workspaces: [root],
    goal: { root_id: "sup1", enabled: true, goal: "Ship the change", state: "done", goal_phase: "ready_for_pr", progress: 1, milestones: [] },
    sessionReview: { sup1: { ready: true, reasons: [], branch: "dev/change", base: "main", diff: "" } },
    sessionBase: { sup1: { [path]: "a = 1\n", "src/brindle/daemon/routes.py": "b = 1\n" } },
    sessionFiles: { sup1: { [path]: "a = 2\n", "src/brindle/daemon/routes.py": "b = 2\n" } },
  });
  const { client, store, sync } = connectDaemon(daemon);
  renderApp(<App client={client} store={store} refresh={sync.refresh} initialRoute={{ screen: "session", root: "sup1", repo: REPO, agent: "sup1" }} />);
  fireEvent.click(await screen.findByRole("button", { name: /Review & open PR/ }));
  const sheet = await screen.findByRole("dialog", { name: "Review & open PR" });
  fireEvent.click(within(sheet).getByTestId("review-changes"));
  const files = await within(sheet).findByRole("navigation", { name: "Changed files" });
  // Nesting costs the names below it their room: a folder holding only a folder is one row, not three.
  expect(within(files).getAllByRole("button", { expanded: true })).toHaveLength(1);
  expect(within(files).getByTitle("src/brindle/daemon/").classList.contains("truncate")).toBe(true);
  // The file header: the folder gives way, the name never does.
  const title = await within(sheet).findByTestId("file-title");
  // A name wider than the pane truncates too (it cannot paint over the controls beside it), but after the folder.
  const [folder, name] = [title.firstElementChild!, title.lastElementChild!];
  expect(name.classList.contains("truncate") && name.classList.contains("min-w-0")).toBe(true);
  expect(folder.classList.contains("truncate") && folder.className).toContain("shrink-[100]");
  // The hunk's header gives way to its action text, never under it.
  const hunk = within(sheet).getAllByTestId("hunk")[0];
  expect(hunk.querySelector("button > span")?.className).toContain("truncate");
  expect(hunk.querySelector("button > span:last-child")?.className).toContain("shrink-0");
  // A line wider than the pane carries its colour to its end.
  expect(hunk.querySelector("pre")?.className).toContain("w-max");
  // The sheet's subtitle and badge wrap, and the branch text breaks, instead of running into each other.
  const pill = within(sheet).getByTestId("review-edit-pill");
  expect(pill.parentElement?.className).toContain("flex-wrap");
  expect(pill.previousElementSibling?.className).toContain("min-w-0");
  // Split: two columns that may not grow past their half, each scrolling its own long lines.
  fireEvent.click(within(sheet).getByRole("button", { name: "Split" }));
  const split = await within(sheet).findByTestId("split-diff");
  expect(split.className).toContain("grid-cols-2");
  for (const side of within(split).getAllByLabelText(/version/)) expect(side.className).toContain("min-w-0 overflow-x-auto");
});

it.each(["inbox", "sidebar"] as const)("opens the ready session's diff from the %s", async (entry) => {
  const daemon = new FakeDaemon({
    repos: [{ repo_root: REPO, name: "proj" }],
    sessions: [{ id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 1, goal: "Ship the change" }],
    workspaces: [root],
    goal: { root_id: "sup1", enabled: true, goal: "Ship the change", state: "done", goal_phase: "ready_for_pr", progress: 1, milestones: [] },
    inbox: [{ kind: "pr_ready", reason: "Ready", root_id: "sup1", agent_id: "sup1", workspace_id: "proj" }],
    sessionReview: { sup1: { ready: true, reasons: [], branch: "dev/change", base: "main", diff: "" } },
    sessionBase: { sup1: { "change.ts": "export const value = 1;\n" } },
    sessionFiles: { sup1: { "change.ts": "export const value = 2;\n" } },
  });
  const { client, store, sync } = connectDaemon(daemon);
  renderApp(<App client={client} store={store} refresh={sync.refresh} initialRoute={entry === "inbox" ? { screen: "inbox" } : { screen: "session", root: "sup1", repo: REPO, agent: "sup1" }} />);

  if (entry === "sidebar") await screen.findByTestId("goal-phase");
  const source = entry === "inbox"
    ? await screen.findByTestId("inbox-card")
    : await screen.findByRole("region", { name: "Session context" });
  fireEvent.click(await within(source).findByRole("button", { name: /Review & open PR/ }));
  const sheet = await screen.findByRole("dialog", { name: "Review & open PR" });
  fireEvent.click(within(sheet).getByTestId("review-changes"));
  const files = await within(sheet).findByRole("navigation", { name: "Changed files" });
  // Narrow: the file list stacks above the diff at full width (names stay readable, the diff isn't clipped); side by side from md up.
  expect(files.className).toContain("w-full");
  expect(files.parentElement?.className).toContain("flex-col");
  expect(files.parentElement?.className).toContain("md:flex-row");
  // A long name gives way (truncates) and the +/− counts keep their room, so they never overlay each other.
  const row = within(files).getByText("change.ts");
  expect(row.classList.contains("truncate") && row.classList.contains("min-w-0")).toBe(true);
  expect(within(files).getByText("+1").classList.contains("shrink-0")).toBe(true);
  expect(within(sheet).getAllByTestId("hunk").length).toBeGreaterThan(0);
  expect(daemon.requests.some((r) => r.method === "POST" && r.path === "/sessions/sup1/pr")).toBe(false);
});
