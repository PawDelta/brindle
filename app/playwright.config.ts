/* npm run test:e2e: the app in real browser engines (e2e/*.e2e.ts), served by
   Vite from shots/preview.html over the fake daemon. WebKit is what Tauri
   runs on macOS (WKWebView) and Linux; Chromium is WebView2's engine on
   Windows. `npx playwright install webkit chromium` fetches them once. */

import { execFileSync } from "node:child_process";
import { defineConfig, devices } from "@playwright/test";

// Each run gets its own port, so two worktrees can run the suite at once.
// E2E_PORT pins it; otherwise a child process asks the OS for a free one.
function freePort(): number {
  if (process.env.E2E_PORT) return Number(process.env.E2E_PORT);
  const probe =
    "const s=require('node:net').createServer();" +
    "s.listen(0,'127.0.0.1',()=>{console.log(s.address().port);s.close()})";
  return Number(execFileSync(process.execPath, ["-e", probe], { encoding: "utf8" }).trim());
}

// Workers re-load this file, so the choice goes in the environment for them to see the same port.
const PORT = freePort();
process.env.E2E_PORT = String(PORT);

export default defineConfig({
  testDir: "e2e",
  testMatch: "*.e2e.ts",
  outputDir: "e2e/out",
  reporter: "list",
  use: { baseURL: `http://127.0.0.1:${PORT}`, viewport: { width: 1440, height: 960 } },
  projects: [
    {
      name: "webkit",
      use: { ...devices["Desktop Safari"], baseURL: `http://127.0.0.1:${PORT}`, viewport: { width: 1440, height: 960 } },
    },
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], baseURL: `http://127.0.0.1:${PORT}`, viewport: { width: 1440, height: 960 } },
    },
  ],
  webServer: {
    command: `npx vite --host 127.0.0.1 --port ${PORT} --strictPort`,
    url: `http://127.0.0.1:${PORT}/shots/preview.html`,
    reuseExistingServer: false,
  },
});
