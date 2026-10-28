// Capture the same provider overview and browser sign-in for a before/after comparison.
// Run from app: node shots/settings-login.mjs before (or after).
import { createServer } from "vite";
import { chromium } from "@playwright/test";

const stage = process.argv[2];
if (!["before", "after"].includes(stage)) throw new Error("Choose before or after");
const server = await createServer({ server: { host: "127.0.0.1", port: 4178 } });
await server.listen();
let browser;
try {
  browser = await chromium.launch({ executablePath: process.argv[3] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 }, deviceScaleFactor: 1 });
  for (const theme of ["light", "dark"]) {
    await page.goto(`${server.resolvedUrls.local[0]}shots/preview.html?scene=settings-login&theme=${theme}`);
    await page.getByTestId("provider-codex").waitFor();
    await page.evaluate(() => document.getElementById("providers").scrollIntoView({ block: "start" }));
    await page.screenshot({ path: `shots/settings-login-${stage}-${theme}.png` });
    if (stage === "after") {
      const keys = page.getByRole("button", { name: "API keys (optional, for advanced setups)", exact: true });
      await keys.click();
      await page.getByRole("form", { name: "Add a key", exact: true }).waitFor();
      await page.screenshot({ path: `shots/settings-login-${stage}-${theme}-keys.png` });
      await keys.click();
    }
    await page.getByRole("button", { name: "Sign in to Codex", exact: true }).click();
    await page.locator('[data-testid="sign-in-flow"][data-phase="waiting"]').waitFor();
    await page.screenshot({ path: `shots/settings-login-${stage}-${theme}-signin.png` });
  }
} finally {
  await browser?.close();
  await server.close();
}
