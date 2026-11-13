import { chromium } from "@playwright/test";
import { mkdir } from "node:fs/promises";

const browser = await chromium.launch({ executablePath: process.argv[2], headless: true });
const out = new URL("../shots/out/review-sheet-pr-first/", import.meta.url);
await mkdir(out, { recursive: true });
try {
  /** @type {Array<"light" | "dark">} */
  const themes = ["light", "dark"];
  for (const theme of themes) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 960 }, colorScheme: theme });
    /** @type {string[]} */
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`http://127.0.0.1:1425/shots/preview.html?scene=session-review&theme=${theme}&renderer=dom`);
    await page.locator('html[data-ready="1"]').waitFor();
    await page.getByLabel("PR title", { exact: true }).filter({ visible: true }).waitFor();
    const inspect = () => page.getByRole("dialog").evaluate((dialog) => ({
      overflow: [...dialog.querySelectorAll("*")].filter((e) => e.clientWidth && e.scrollWidth > e.clientWidth + 1 && e.ownerDocument.defaultView?.getComputedStyle(e).overflowX === "visible").map((e) => e.tagName),
      scrollbars: [...dialog.querySelectorAll("*")].filter((e) => e.clientHeight && e.scrollHeight > e.clientHeight + 1 && /auto|scroll/.test(e.ownerDocument.defaultView?.getComputedStyle(e).overflowY ?? "")).map((e) => e.tagName),
      hiddenDiffHeight: dialog.querySelector('[aria-label="Changed files"]')?.getBoundingClientRect().height ?? 0,
    }));
    console.log(theme, "PR", await inspect());
    await page.screenshot({ path: new URL(`pr-${theme}.png`, out).pathname });
    await page.getByTestId("review-changes").click();
    await page.getByRole("navigation", { name: "Changed files" }).waitFor();
    console.log(theme, "diff", await inspect());
    await page.screenshot({ path: new URL(`diff-${theme}.png`, out).pathname });
    await page.getByTestId("open-pr-step").click();
    console.log(theme, "back", await inspect(), "errors", errors);
    await page.setViewportSize({ width: 1280, height: 720 });
    console.log(theme, "short desktop", await inspect());
    await page.close();
  }
} finally {
  await browser.close();
}
