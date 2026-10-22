#!/usr/bin/env node
/* npm run notices: writes src-tauri/notices/THIRD_PARTY_NOTICES.txt, which the
   bundle carries beside brindle's own LICENSE and SCHEDULE-A (tauri.conf.json's
   bundle.resources). It lists everything else the bundle ships, each with its
   license field, then every license text they come with, each text once with
   the packages it covers:
   - the npm packages the front end ships (package-lock.json's non-dev ones),
   - the Rust crates the shell links (`cargo metadata`'s normal dependencies,
     for TAURI_ENV_TARGET_TRIPLE when Tauri sets it),
   - when src-tauri/engine/ exists (macOS: scripts/build_engine.sh), the
     engine's Python packages (their *.dist-info) and the Python, tmux,
     libevent and utf8proc it carries (engine/licenses/).
   Tauri's beforeBuildCommand runs it before every bundle. */

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP = resolve(HERE, "..");
const OUT = join(APP, "src-tauri/notices/THIRD_PARTY_NOTICES.txt");
const LICENSE_FILE = /^(licen[cs]e|copying|notice|copyright)/i;

/** @typedef {{ kind: string, name: string, version: string, license: string, texts: string[] }} Entry */

/**
 * The license files directly in `dir` (or, with `deep`, every file anywhere under it).
 * @param {string} dir @param {boolean} [deep] @returns {string[]}
 */
function licenseTexts(dir, deep = false) {
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .sort()
    .flatMap((name) => {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) return deep ? licenseTexts(path, true) : [];
      return deep || LICENSE_FILE.test(name) ? [readFileSync(path, "utf8").trim()] : [];
    });
}

/** @param {string} app @returns {Entry[]} */
function npmEntries(app) {
  const lock = JSON.parse(readFileSync(join(app, "package-lock.json"), "utf8"));
  return Object.entries(lock.packages ?? {})
    .filter(([path, pkg]) => path.startsWith("node_modules/") && !pkg.dev && !pkg.devOptional)
    .map(([path, pkg]) => ({
      kind: "npm",
      name: path.slice(path.lastIndexOf("node_modules/") + "node_modules/".length),
      version: pkg.version ?? "",
      license: pkg.license ?? "unknown",
      texts: licenseTexts(join(app, path)),
    }));
}

/** The crates the root package links: its normal (not dev or build) dependencies, transitively. */
/** @param {any} metadata @returns {Entry[]} */
function cargoEntries(metadata) {
  const nodes = new Map(metadata.resolve.nodes.map((/** @type {any} */ n) => [n.id, n]));
  const packages = new Map(metadata.packages.map((/** @type {any} */ p) => [p.id, p]));
  const seen = new Set();
  const queue = [metadata.resolve.root];
  while (queue.length) {
    const node = nodes.get(queue.pop());
    for (const dep of node?.deps ?? []) {
      if (seen.has(dep.pkg) || !dep.dep_kinds.some((/** @type {any} */ k) => k.kind === null)) continue;
      seen.add(dep.pkg);
      queue.push(dep.pkg);
    }
  }
  return [...seen].map((id) => {
    const pkg = packages.get(id);
    return {
      kind: "crate",
      name: pkg.name,
      version: pkg.version,
      license: pkg.license ?? (pkg.license_file ? `see ${pkg.license_file}` : "unknown"),
      texts: licenseTexts(dirname(pkg.manifest_path)),
    };
  });
}

/** @param {string} engine @returns {Entry[]} */
function engineEntries(engine) {
  const lib = join(engine, "python/lib");
  const pylib = existsSync(lib) ? readdirSync(lib).find((d) => /^python3\.\d+$/.test(d)) : undefined;
  const site = pylib ? join(lib, pylib, "site-packages") : "";
  const python = (site && existsSync(site) ? readdirSync(site) : [])
    .filter((d) => d.endsWith(".dist-info"))
    .map((d) => {
      const meta = readFileSync(join(site, d, "METADATA"), "utf8");
      const field = (/** @type {string} */ key) => meta.match(new RegExp(`^${key}: (.*)$`, "m"))?.[1]?.trim();
      return {
        kind: "python",
        name: field("Name") ?? d,
        version: field("Version") ?? "",
        license: field("License-Expression") ?? field("License") ?? "unknown",
        texts: [...licenseTexts(join(site, d)), ...licenseTexts(join(site, d, "licenses"), true)],
      };
    })
    .filter((e) => e.name !== "brindle");
  const licenses = join(engine, "licenses");
  const bundled = (existsSync(licenses) ? readdirSync(licenses).sort() : []).map((f) => ({
    kind: "engine",
    name: basename(f, ".txt"),
    version: "",
    license: "see its text",
    texts: [readFileSync(join(licenses, f), "utf8").trim()],
  }));
  return [...bundled, ...python];
}

/** The notices file: every entry with its license field, then each distinct license text once. */
/** @param {string} title @param {Entry[]} entries */
export function render(title, entries) {
  const label = (/** @type {Entry} */ e) => `${e.name}${e.version ? ` ${e.version}` : ""} (${e.kind})`;
  const sorted = [...entries].sort((a, b) => a.kind.localeCompare(b.kind) || a.name.localeCompare(b.name));
  /** @type {Map<string, string[]>} */
  const byText = new Map();
  for (const e of sorted) for (const text of new Set(e.texts)) byText.set(text, [...(byText.get(text) ?? []), label(e)]);
  const rule = "-".repeat(78);
  return [
    title,
    "",
    "brindle itself is under the Brindle License (LICENSE and SCHEDULE-A, beside this file).",
    "It ships the third-party software below, under these licenses.",
    "",
    ...sorted.map((e) => `${label(e)}: ${e.license}${e.texts.length ? "" : " (no license file shipped)"}`),
    "",
    ...[...byText].flatMap(([text, who]) => [rule, `${who.join(", ")}:`, "", text, ""]),
  ].join("\n");
}

/** @param {string} app @param {any} metadata */
export function collect(app, metadata) {
  const engine = join(app, "src-tauri/engine");
  return [...npmEntries(app), ...cargoEntries(metadata), ...(existsSync(engine) ? engineEntries(engine) : [])];
}

function main() {
  const target = process.env.TAURI_ENV_TARGET_TRIPLE;
  const metadata = JSON.parse(
    execFileSync(
      "cargo",
      ["metadata", "--format-version", "1", "--manifest-path", join(APP, "src-tauri/Cargo.toml"), ...(target ? ["--filter-platform", target] : [])],
      { encoding: "utf8", maxBuffer: 256 * 1024 * 1024 },
    ),
  );
  const version = JSON.parse(readFileSync(join(APP, "package.json"), "utf8")).version;
  const entries = collect(APP, metadata);
  mkdirSync(dirname(OUT), { recursive: true });
  writeFileSync(OUT, render(`Third-party notices for brindle ${version}`, entries));
  console.log(`notices: wrote ${OUT} (${entries.length} packages)`);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
