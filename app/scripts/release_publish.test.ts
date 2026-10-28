// @vitest-environment node
import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const cli = fileURLToPath(new URL("./release_publish.mjs", import.meta.url));

/** release_publish.mjs run as the workflow runs it (async: the fake API answers from this process). */
function run(args: string[], env: Record<string, string>, cwd?: string): Promise<{ status: number | null; out: string }> {
  const { GITHUB_OUTPUT: _, ...inherited } = process.env;
  const child = spawn("node", [cli, ...args], { cwd, env: { ...inherited, ...env } });
  let out = "";
  child.stdout.on("data", (chunk) => (out += chunk));
  child.stderr.on("data", (chunk) => (out += chunk));
  return new Promise((done) => child.on("close", (status) => done({ status, out })));
}

/** A repository whose main is A-B-C, with a side commit X off A; returns their full SHAs. */
function repo() {
  const dir = mkdtempSync(join(tmpdir(), "release-repo-"));
  const git = (...args: string[]) =>
    spawnSync("git", ["-c", "user.name=t", "-c", "user.email=t@t", ...args], { cwd: dir, encoding: "utf8" }).stdout.trim();
  git("init", "-q", "-b", "main");
  const commit = (m: string) => (git("commit", "-q", "--allow-empty", "-m", m), git("rev-parse", "HEAD"));
  const a = commit("A");
  git("checkout", "-q", "-b", "side");
  const x = commit("X");
  git("checkout", "-q", "main");
  const b = commit("B");
  const c = commit("C");
  return { dir, a, b, c, x };
}

const R = repo();

describe("ci-gate: App (and CI when it ran), newest run for exactly the commit, on main", () => {
  const sha = R.c;
  type Run = { id: number; head_sha: string; head_branch: string; event: string; status: string; conclusion: string | null; created_at: string; run_attempt: number };
  const green = (id: number, over: Partial<Run> = {}): Run => ({
    id, head_sha: sha, head_branch: "main", event: "push", status: "completed", conclusion: "success",
    created_at: `2026-10-23T10:00:${String(id).padStart(2, "0")}Z`, run_attempt: 1, ...over,
  });
  /** Each workflow's runs, as pages of the runs list; a list of them answers the gate's polls in turn (the last repeats). */
  type Runs = Record<string, Run[][]>;
  let pages: Runs | Runs[] = {};
  let polls: Record<string, number> = {};
  let server: Server;
  let api = "";

  beforeAll(async () => {
    server = createServer((req, res) => {
      const url = new URL(req.url ?? "", api);
      const page = Number(url.searchParams.get("page") ?? "1");
      const following = new URL(url);
      following.searchParams.set("page", String(page + 1));
      const next = (more: boolean) => (more ? { link: `<${following}>; rel="next"` } : {});
      const workflows = url.pathname.match(/^\/repos\/o\/r\/actions\/workflows(?:\/(\w+)\/runs)?$/);
      if (!workflows || req.headers.authorization !== "Bearer t") return res.writeHead(404).end();
      if (!workflows[1]) {
        // Two pages of workflows: the gate must find App on the second.
        const all = [
          [{ id: 1, path: ".github/workflows/ci.yml", state: "active" }],
          [{ id: 2, path: ".github/workflows/app.yml", state: "active" }, { id: 3, path: ".github/workflows/app-release.yml", state: "active" }],
        ];
        res.writeHead(200, { "content-type": "application/json", ...next(page < all.length) });
        return res.end(JSON.stringify({ workflows: all[page - 1] }));
      }
      const key = workflows[1] === "1" ? "ci" : "app";
      if (page === 1) polls[key] = (polls[key] ?? -1) + 1;
      const poll = Array.isArray(pages) ? pages[Math.min(polls[key], pages.length - 1)] : pages;
      const runs = poll[key] ?? [[]];
      res.writeHead(200, { "content-type": "application/json", ...next(page < runs.length) });
      res.end(JSON.stringify({ workflow_runs: runs[page - 1] ?? [] }));
    });
    await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
    api = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  });
  afterAll(() => server.close());

  const running = green(2, { status: "in_progress", conclusion: null });
  it.each<[string, Runs | Runs[], string, string | null]>([
    ["both passed", { ci: [[green(1)]], app: [[green(2)]] }, sha, null],
    ["App's run on its second page", { ci: [[green(1)]], app: [[green(9, { head_sha: R.b })], [green(2)]] }, sha, null],
    ["no CI run (an app-only push): App alone", { app: [[green(2)]] }, sha, null],
    ["App still running at the deadline", { ci: [[green(1)]], app: [[running]] }, sha, "still waiting after 0 minutes: .github/workflows/app.yml run 2 (attempt 1) is in_progress"],
    ["App finishes while the gate waits", [{ ci: [[green(1)]], app: [[running]] }, { ci: [[green(1)]], app: [[green(2)]] }], sha, null],
    ["CI's newest run failed after an older one passed", { ci: [[green(1), green(5, { conclusion: "failure", run_attempt: 2 })]], app: [[green(2)]] }, sha, "(attempt 2) ended failure"],
    ["only a pull request's and another branch's App passed", { ci: [[green(1)]], app: [[green(2, { event: "pull_request" }), green(3, { head_branch: "release/desktop" })]] }, sha, "app.yml has no run on main"],
    ["a commit that is not on main", { ci: [[green(1, { head_sha: R.x })]], app: [[green(2, { head_sha: R.x })]] }, R.x, "is not on main"],
  ])("%s", async (_case, runs, commit, refusal) => {
    pages = runs;
    polls = {};
    // A gate that has to wait polls at once (interval 0); one that never sees an end gives up at its first look (deadline 0).
    const deadline = Array.isArray(runs) ? "1" : "0";
    const env = {
      GITHUB_API_URL: api, GITHUB_REPOSITORY: "o/r", GITHUB_TOKEN: "t", GITHUB_SHA: commit, MAIN_REF: "main",
      CI_GATE_DEADLINE_MINUTES: deadline, CI_GATE_INTERVAL_SECONDS: "0",
    };
    const { status, out } = await run(["ci-gate"], env, R.dir);
    if (refusal === null) {
      expect(status, out).toBe(0);
      expect(out).toContain("go=true");
    } else {
      expect(status, out).toBe(1);
      expect(out).toContain("go=false");
      expect(out).toContain(refusal);
    }
  });
});

describe("a release-branch build's version, files and latest.json", () => {
  const short = (sha: string) => sha.slice(0, 12);
  const manifest = (dir: string, name: string, version: string) => {
    const path = join(dir, name);
    writeFileSync(path, JSON.stringify({ version, platforms: {} }));
    return path;
  };

  it.each([
    ["none published yet", null, `0.1.0-rc.9+release.5.${short(R.b)}`, true],
    ["a later run at a descendant", `0.1.0-rc.9+release.4.${short(R.a)}`, `0.1.0-rc.9+release.5.${short(R.c)}`, true],
    ["rc.9 to rc.10 (numeric, not text, order)", `0.1.0-rc.9+release.4.${short(R.a)}`, `0.1.0-rc.10+release.5.${short(R.b)}`, true],
    ["a release candidate to its release", `0.1.0-rc.9+release.4.${short(R.a)}`, `0.1.0+release.5.${short(R.b)}`, true],
    ["a stale run finishing after a later one", `0.1.0-rc.9+release.6.${short(R.c)}`, `0.1.0-rc.9+release.5.${short(R.b)}`, false],
    ["the same run again (a re-run)", `0.1.0-rc.9+release.5.${short(R.b)}`, `0.1.0-rc.9+release.5.${short(R.b)}`, false],
    ["a lower version in a later run", `0.1.0-rc.10+release.4.${short(R.a)}`, `0.1.0-rc.9+release.5.${short(R.b)}`, false],
    ["a commit that does not descend from the published one", `0.1.0-rc.9+release.4.${short(R.b)}`, `0.1.0-rc.9+release.5.${short(R.x)}`, false],
  ])("promote: %s", async (_case, current, next, go) => {
    const dir = mkdtempSync(join(tmpdir(), "promote-"));
    const currentFile = current ? manifest(dir, "current.json", current) : join(dir, "absent.json");
    const { status, out } = await run(["promote", currentFile, manifest(dir, "next.json", next)], {}, R.dir);
    expect(status, out).toBe(0);
    expect(out).toContain(`go=${go}`);
  });

  it("promote refuses to replace a published latest.json that is not a release-branch build", async () => {
    const dir = mkdtempSync(join(tmpdir(), "promote-"));
    const { status, out } = await run(
      ["promote", manifest(dir, "current.json", "0.1.0"), manifest(dir, "next.json", `0.1.0+release.5.${short(R.b)}`)], {}, R.dir,
    );
    expect(status).toBe(1);
    expect(out).toContain("the published 0.1.0 is not a release-branch build");
  });

  /** Both Mac builds' collected files and status, as the macos job leaves them, for the identity `id`. */
  function builds(id: Record<string, string>, over: Record<string, Record<string, unknown>> = {}) {
    const dir = mkdtempSync(join(tmpdir(), "verify-"));
    mkdirSync(join(dir, id.name));
    for (const arch of ["aarch64", "x86_64"]) {
      const stem = join(dir, id.name, `brindle_${id.name}_${arch}`);
      for (const ext of [".dmg", ".app.tar.gz"]) writeFileSync(stem + ext, "");
      writeFileSync(`${stem}.app.tar.gz.sig`, `sig-${arch}\n`);
      const status = { version: id.version, arch, plist_version: id.version, app_version: id.version, signed: true, notarized: true, updater: true };
      writeFileSync(join(dir, `macos-${arch}.status.json`), JSON.stringify({ ...status, ...over[arch] }));
    }
    return dir;
  }

  it.each([
    ["both builds signed, notarized and at the identity's version", {}, null],
    ["an Info.plist that is not the identity's version", { x86_64: { plist_version: "0.1.0-rc.9" } }, "the x86_64 build's plist_version is 0.1.0-rc.9"],
    ["a binary built at another version", { aarch64: { app_version: "0.1.0-rc.9" } }, "the aarch64 build's app_version is 0.1.0-rc.9"],
    ["a build that is not notarized", { aarch64: { notarized: false } }, "the aarch64 build is not notarized"],
    // Re-running the publish job alone: release-gate's outputs (its path) are attempt 1's, this attempt is 2.
    ["a re-run given an earlier attempt's directory", { path: { attempt: 1 } }, "is not this attempt's (2) directory"],
  ])("verify: %s", async (_case, over, refusal) => {
    const sha = R.c;
    const github = { GITHUB_SHA: sha, GITHUB_RUN_NUMBER: "42", GITHUB_RUN_ATTEMPT: "2", GITHUB_RUN_ID: "900" };
    const identity = await run(["identity"], github);
    expect(identity.status, identity.out).toBe(0);
    const id = Object.fromEntries(identity.out.trim().split("\n").map((line) => line.split(/=(.*)/s).slice(0, 2)));
    const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version;
    expect(id).toEqual({ version: `${pkg}+release.42.${short(sha)}`, name: `${pkg}-release.42.${short(sha)}`, path: `release/42.2-${short(sha)}` });

    const dir = builds(id, over);
    const site = "https://pawdelta.com/brindle/downloads/desktop";
    const dest = "path" in over ? id.path.replace(".2-", ".1-") : id.path;
    const env = { ...github, VERSION: id.version, NAME: id.name, DEST_PATH: dest, SITE_DOWNLOADS: site };
    const { status, out } = await run(["verify", dir], env);
    if (refusal) {
      expect(status, out).toBe(1);
      expect(out).toContain(refusal);
      return;
    }
    expect(status, out).toBe(0);
    const latest = JSON.parse(readFileSync(join(dir, id.name, "latest.json"), "utf8"));
    expect(latest.version).toBe(id.version);
    expect(latest.notes).toContain(sha);
    expect(latest.platforms["darwin-x86_64"]).toEqual({ signature: "sig-x86_64", url: `${site}/${id.path}/brindle_${id.name}_x86_64.app.tar.gz` });
    expect(JSON.parse(readFileSync(join(dir, id.name, "build.json"), "utf8"))).toMatchObject({ version: id.version, package_version: pkg, commit: sha, run_attempt: "2" });
  });
});
