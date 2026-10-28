/* Three flows in a real browser engine, over the fake daemon (shots/preview.html)
   alone: no tmux, no Claude Code, so they run in CI. The page exposes the fake
   as `window.fake`: what a pane view wrote to its pty, and `print` into a pane. */

import { expect, test, type Page } from "@playwright/test";

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

test("New Session: the project search filters, and closes on an outside click and on Esc", async ({ page }) => {
  await open(page, "new");
  const repo = page.getByRole("combobox", { name: "Repo" });
  const list = page.getByRole("listbox", { name: "Projects" });

  await repo.click();
  await repo.fill("paw");
  await expect(list.getByRole("option")).toHaveText(["pawdelta"]);

  await page.getByRole("region", { name: "New session" }).click({ position: { x: 4, y: 4 } });
  await expect(list).toHaveCount(0);

  await repo.click();
  await expect(list).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(list).toHaveCount(0);
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
  // (forced: xterm's screen layer sits over its rows and takes the mouse)
  await link.click({ force: true });
  expect(await opened()).toEqual([]);
  await link.click({ force: true, modifiers: [mac ? "Meta" : "Control"] });
  await expect.poll(opened).toEqual(["https://example.com/pr/7"]);
});
