/* The demo build (npm run build:demo): the real app over the in-memory
   fake daemon, as one static page that opens from file:// with no server.
   A browser refuses module scripts from file:// (origin "null"), so the
   bundle is built as a classic script and inlined, with the stylesheet,
   into index.html once Vite has written them: one file, double-click it.
   Nothing of it ships: `vite build` bundles the app's index.html only. */
import { readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

function inlineIntoIndex(): Plugin {
  let outDir = "";
  return {
    name: "demo-inline",
    configResolved(config) {
      outDir = config.build.outDir;
    },
    closeBundle() {
      const assets = join(outDir, "assets");
      let html = readFileSync(join(outDir, "index.html"), "utf8");
      for (const file of readdirSync(assets)) {
        const text = readFileSync(join(assets, file), "utf8");
        if (file.endsWith(".js")) {
          // A classic script runs where it stands: at the foot, once #root exists.
          html = html.replace(/<script[^>]*src="\.\/assets\/[^"]+"[^>]*><\/script>\n?/, "");
          html = html.replace(/<\/body>/, () => `<script>${text.replace(/<\/(script)/gi, "<\\/$1")}</script>\n</body>`);
        } else if (file.endsWith(".css")) {
          html = html.replace(/<link rel="stylesheet"[^>]*href="\.\/assets\/[^"]+"[^>]*>/, () => `<style>${text}</style>`);
        }
      }
      writeFileSync(join(outDir, "index.html"), html);
      rmSync(assets, { recursive: true, force: true });
    },
  };
}

export default defineConfig({
  root: __dirname,
  base: "./",
  plugins: [react(), tailwindcss(), inlineIntoIndex()],
  build: {
    outDir: resolve(__dirname, "dist"),
    emptyOutDir: true,
    target: "safari15",
    modulePreload: false,
    cssCodeSplit: false,
    chunkSizeWarningLimit: 2000,
    rolldownOptions: { output: { format: "iife", inlineDynamicImports: true } },
  },
});
