/* Three flows in a real browser engine, over the fake daemon (shots/preview.html)
   alone: no tmux, no Claude Code, so they run in CI. The page exposes the fake
   as `window.fake`: what a pane view wrote to its pty, and `print` into a pane. */

import { expect, test, type Locator, type Page } from "@playwright/test";

const WORKER = "a7aaa807";

type Fake = { views: { pane: string | null; written: string[] }[]; print(pane: string, text: string): void };

const open = async (page: Page, scene: string) => {
  await page.addInitScript(() => {
    // A plain tab's opener (api/open.ts openExternal) is window.open: record it instead of opening a tab.
    const opened: string[] = [];
    Object.assign(window, { opened });
    window.open = (url) => (opened.push(String(url)), null);
  });
  await page.goto(`/shots/preview.html?scene=${scene}&theme=light&renderer=dom`);
  await page.waitForSelector("html[data-ready]");
};

test("sidebar keeps its goal pinned and its lists reachable when resized", async ({ page }, testInfo) => {
  await open(page, "sidebar-resize");
  const sidebar = page.getByRole("complementary", { name: "Sidebar" });
  const goal = sidebar.getByRole("region", { name: "Goal", exact: true });
  const line = goal.getByTestId("goal-line");
  const count = goal.getByTestId("goal-count");
  const bar = goal.getByRole("progressbar");
  const milestones = goal.getByTestId("goal-scroll");
  const sessions = sidebar.getByRole("navigation", { name: "Sessions", exact: true });
  const box = async (locator: Locator) => (await locator.boundingBox())!;
  // Visibility here means inside every clipping ancestor, not merely a nonzero DOM box.
  const fullyInView = async (locator: Locator) => {
    await expect(locator).toBeInViewport({ ratio: 1 });
    expect(await locator.evaluate((el) => {
      const rect = el.getBoundingClientRect();
      for (let parent = el.parentElement; parent; parent = parent.parentElement) {
        const clip = parent.getBoundingClientRect();
        const css = getComputedStyle(parent);
        if (/(auto|scroll|hidden|clip)/.test(css.overflowX) && (rect.left < clip.left - 1 || rect.right > clip.right + 1)) return false;
        if (/(auto|scroll|hidden|clip)/.test(css.overflowY) && (rect.top < clip.top - 1 || rect.bottom > clip.bottom + 1)) return false;
      }
      return true;
    })).toBe(true);
  };

  await expect(count).toHaveText("9 of 15");
  await count.click();
  await goal.getByRole("button", { name: "5 pending" }).click();
  for (const [name, width, height] of [["normal", 1440, 960], ["short", 1440, 420], ["narrow", 760, 960]] as const) {
    await page.setViewportSize({ width, height });
    await milestones.evaluate((el) => { el.scrollTop = 0; });
    const lineHeight = await line.evaluate((el) => parseFloat(getComputedStyle(el).lineHeight));
    expect((await box(line)).height).toBeLessThanOrEqual(2 * lineHeight + 1); // the goal title clamps to two lines
    await fullyInView(line);
    await fullyInView(count);
    expect(await count.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
    await fullyInView(bar);
    const pinned = await box(goal.getByTestId("goal-header"));
    expect(await milestones.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
    await milestones.evaluate((el) => { el.scrollTop = el.scrollHeight; });
    expect(await milestones.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
    await fullyInView(goal.getByTestId("milestone").last());
    expect(await box(goal.getByTestId("goal-header"))).toEqual(pinned);
    await fullyInView(line);
    await fullyInView(bar);

    await fullyInView(sessions);
    expect((await box(sessions)).height).toBeGreaterThan(30);
    await sessions.evaluate((el) => { el.scrollTop = el.scrollHeight; });
    await fullyInView(sessions.locator("button[data-row]").last());
    await fullyInView(sidebar.getByTestId("sidebar-foot"));
    await fullyInView(sidebar.getByRole("group", { name: "Sidebar foot" }));
    expect(await sidebar.evaluate((el) => [el, ...el.querySelectorAll<HTMLElement>("*")]
      .filter((node) => node.clientWidth > 0 && getComputedStyle(node).overflowX !== "hidden")
      .filter((node) => node.scrollWidth > node.clientWidth + 1)
      .map((node) => node.className))).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath(`sidebar-${name}.png`) });
  }
});

test("sidebar foot pill stays compact in calm and busy states", async ({ page }, testInfo) => {
  for (const state of ["calm", "busy"]) {
    await open(page, `sidebar-foot-${state}`);
    const foot = page.getByTestId("sidebar-foot");
    const pill = foot.getByTestId("profile-pill");
    const busy = state === "busy";
    await expect(pill).toContainText(busy ? "88%" : "41%");
    await expect(foot.getByTestId("provider-problem")).toHaveCount(0);
    await expect(pill.getByTestId("usage-warning-dot")).toHaveCount(busy ? 1 : 0);
    await expect(foot.getByTestId("app-update-pill")).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath(`sidebar-foot-${state}.png`) });
    // The pill and strip shrink inside even a sidebar narrower than the app's usual rail.
    await page.getByRole("complementary", { name: "Sidebar" }).evaluate((el) => { el.style.width = "190px"; });
    expect(await foot.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
    await pill.click();
    await expect(page.getByRole("tab", { name: "Usage", exact: true })).toHaveAttribute("aria-selected", "true");
  }
});

test("sidebar: a deep worker tree stays inside the list, and a tall split leaves the list in reach", async ({ page }) => {
  await open(page, "sidebar-resize");
  await page.evaluate(() => {
    type Ws = { id: string; path: string; agents: { id: string; parent_id?: string | null; mode: string }[] };
    const { fake } = window as unknown as { fake: { data: { workspaces: Ws[] }; publish(kind: string, fields: object): void } };
    const home = fake.data.workspaces.find((w) => w.id === "pawdelta")!;
    const agents = Array.from({ length: 12 }, (_, i) => ({ ...home.agents[0], id: `deep${i}`, mode: "worker", parent_id: i ? `deep${i - 1}` : "sup-42" }));
    fake.data.workspaces.push({ ...home, id: "pawdelta/deep", path: `${home.path}/.brindle/worktrees/deep`, agents });
    fake.publish("agent.status", { agent_id: "not-listed-yet", payload: {} }); // the store reads the snapshot again
  });
  const sessions = page.getByRole("navigation", { name: "Sessions", exact: true });
  const deepest = sessions.locator('[data-tree-agent="deep11"]');
  await expect(deepest).toBeVisible();
  expect(await sessions.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
  expect((await deepest.boundingBox())!.width).toBeGreaterThan(120);

  const handle = (await page.getByTestId("split-handle").boundingBox())!;
  await page.mouse.move(handle.x + handle.width / 2, handle.y + 4);
  await page.mouse.down();
  await page.mouse.move(handle.x + handle.width / 2, 950, { steps: 4 });
  await page.mouse.up();
  await page.setViewportSize({ width: 1200, height: 480 }); // the app window's minimum height
  await expect.poll(async () => (await sessions.boundingBox())!.height).toBeGreaterThanOrEqual(70);
});

test("New Session: the project chip's menu closes on an outside click and on Esc", async ({ page }) => {
  await open(page, "new");
  await expect(page.getByRole("button", { name: "Start the demo" })).toHaveCount(0); // the offer stays hidden once decided
  const chip = page.getByRole("button", { name: /^Project:/ });
  const menu = page.getByRole("menu", { name: "Project" });

  await chip.click();
  await expect(menu.getByRole("menuitemradio", { name: /^pawdelta/ })).toHaveAttribute("aria-checked", "true");

  await page.getByRole("region", { name: "New session" }).click({ position: { x: 4, y: 4 } });
  await expect(menu).toHaveCount(0);

  await chip.click();
  await expect(menu).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
});

test("session terminal: a paste reaches the pty, and a printed URL opens in the app's opener", async ({ page }) => {
  await open(page, "session");
  const terminal = page.getByTestId("terminal").first();
  await expect(terminal).toHaveAttribute("data-view", "live");
  const written = () => page.evaluate((pane) => (window as unknown as { fake: Fake }).fake.views.filter((v) => v.pane === pane).flatMap((v) => v.written).join(""), WORKER);

  // Pasted text goes to the pane's pty, as one paste.
  await terminal.click();
  await terminal.evaluate((el) => {
    const data = new DataTransfer();
    data.setData("text/plain", "echo pasted-from-clipboard");
    el.querySelector("textarea")!.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
  });
  await expect.poll(written).toContain("echo pasted-from-clipboard");

  // A URL the pane prints is a link: a plain click is the pane's, ⌘-click (Ctrl off a Mac) opens it.
  await page.evaluate((pane) => (window as unknown as { fake: Fake }).fake.print(pane, "\r\nsee https://example.com/pr/7 for it\r\n"), WORKER);
  const link = terminal.getByText("https://example.com/pr/7");
  await expect(link).toBeVisible();
  const mac = await page.evaluate(() => /Mac|iPhone|iPad/.test(navigator.platform));
  const opened = () => page.evaluate(() => (window as unknown as { opened: string[] }).opened);
  // By position, as a person's mouse: xterm's screen layer sits over its rows and takes
  // the mouse, and a redraw can leave a locator's span with no size at the moment it clicks.
  const box = (await link.boundingBox())!;
  const at = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  await page.mouse.click(at.x, at.y);
  expect(await opened()).toEqual([]);
  const modifier = mac ? "Meta" : "Control";
  await page.keyboard.down(modifier);
  await page.mouse.click(at.x, at.y);
  await page.keyboard.up(modifier);
  await expect.poll(opened).toEqual(["https://example.com/pr/7"]);
});
