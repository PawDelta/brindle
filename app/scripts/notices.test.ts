// @vitest-environment node
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { expect, it } from "vitest";
import { collect, render } from "./notices.mjs";

it("lists what the bundle ships, not dev-only packages or brindle, with each license text once", () => {
  const app = mkdtempSync(join(tmpdir(), "notices-"));
  const file = (path: string, text: string) => {
    mkdirSync(dirname(join(app, path)), { recursive: true });
    writeFileSync(join(app, path), text);
  };
  file("package-lock.json", JSON.stringify({ packages: {
    "": { name: "brindle-desktop" },
    "node_modules/react": { version: "19.0.0", license: "MIT" },
    "node_modules/vitest": { version: "4.0.0", license: "MIT", dev: true },
  } }));
  file("node_modules/react/LICENSE", "MIT text");
  file("node_modules/react/README.md", "not a license");
  file("crates/serde/LICENSE-MIT", "MIT text");
  file("crates/serde/LICENSE-APACHE", "Apache text");
  const crate = (id: string) => ({ id, name: id, version: "1.0.0", license: "MIT OR Apache-2.0", manifest_path: join(app, "crates", id, "Cargo.toml") });
  const normal = (pkg: string) => ({ pkg, dep_kinds: [{ kind: null }] });
  const metadata = {
    packages: ["shell", "serde", "itoa", "tempfile"].map(crate),
    resolve: { root: "shell", nodes: [
      { id: "shell", deps: [normal("serde"), { pkg: "tempfile", dep_kinds: [{ kind: "dev" }] }] },
      { id: "serde", deps: [normal("itoa")] },
    ] },
  };
  const site = "src-tauri/engine/python/lib/python3.13/site-packages";
  file("src-tauri/engine/licenses/tmux.txt", "ISC text");
  file(`${site}/typer-0.27.2.dist-info/METADATA`, "Name: typer\nVersion: 0.27.2\nLicense-Expression: MIT\n");
  file(`${site}/typer-0.27.2.dist-info/licenses/LICENSE`, "typer's license");
  file(`${site}/brindle-0.4.4.dist-info/METADATA`, "Name: brindle\nVersion: 0.4.4\n");

  const out = render("Notices", collect(app, metadata));
  expect(out).toContain("react 19.0.0 (npm): MIT\n");
  expect(out).toContain("serde 1.0.0 (crate): MIT OR Apache-2.0\n");
  expect(out).toContain("itoa 1.0.0 (crate): MIT OR Apache-2.0 (no license file shipped)\n");
  expect(out).toContain("typer 0.27.2 (python): MIT\n");
  expect(out).toContain("tmux (engine): see its text\n");
  for (const left of ["vitest", "tempfile", "shell 1.0.0", "brindle 0.4.4", "not a license"]) expect(out).not.toContain(left);
  expect(out.split("MIT text").length).toBe(2);
  expect(out).toContain("serde 1.0.0 (crate), react 19.0.0 (npm):\n\nMIT text\n");
  expect(out).toContain("typer 0.27.2 (python):\n\ntyper's license\n");
});
