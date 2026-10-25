#!/usr/bin/env node
/* release_publish.mjs — the release-branch (staff) builds of app-release.yml.
   A push to release/desktop builds the Macs at that commit, signed and
   notarized like a release, and publishes them under
   brindle/downloads/desktop/release/ only (app/README.md, "Release-branch
   builds"). Subcommands, each reading the GitHub Actions environment:

   ci-gate   the commit (GITHUB_SHA) must be on main (MAIN_REF, default
             origin/main) and the newest run of BOTH the CI and the App
             workflows for exactly that commit, on main, must have passed.
   identity  the build's app version <package.json version>+release.<run>.<sha12>,
             its file stem and its immutable directory.
   verify    both Mac builds' status files in DIR must be signed, notarized,
             carry an updater bundle and the identity's version (in the
             signed Info.plist and the binary); writes latest.json and
             build.json into DIR/<name>.
   promote   whether the new latest.json may replace the published one:
             a strictly later run of a version no lower, at a descendant of
             its commit. The workflow writes it with an S3 conditional put.
   oidc-token  GitHub's OIDC token for the AWS role (audience sts.amazonaws.com).

   Each prints key=value lines, appended to $GITHUB_OUTPUT when that is set. */

import { spawnSync } from "node:child_process";
import { appendFileSync, existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ARCHES = ["aarch64", "x86_64"];
/** The workflows a release-branch build needs green for its commit, by path. */
export const REQUIRED = [".github/workflows/ci.yml", ".github/workflows/app.yml"];
const IDENTITY = /^release\.(\d+)\.([0-9a-f]{12})$/;

/**
 * A semver version's parts, or null.
 * @param {string} text
 */
export function parseVersion(text) {
  const m = /^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$/.exec(text ?? "");
  if (!m) return null;
  const build = m[5] ?? "";
  const release = IDENTITY.exec(build);
  return {
    core: [Number(m[1]), Number(m[2]), Number(m[3])],
    pre: m[4] ? m[4].split(".") : [],
    build,
    run: release ? Number(release[1]) : null,
    sha: release ? release[2] : null,
  };
}

/**
 * Semver precedence of two parsed versions (build metadata ignored): <0, 0 or >0.
 * @param {NonNullable<ReturnType<typeof parseVersion>>} a
 * @param {NonNullable<ReturnType<typeof parseVersion>>} b
 */
export function precedence(a, b) {
  for (let i = 0; i < 3; i++) if (a.core[i] !== b.core[i]) return a.core[i] - b.core[i];
  if (!a.pre.length || !b.pre.length) return (a.pre.length ? -1 : 0) + (b.pre.length ? 1 : 0);
  for (let i = 0; i < Math.max(a.pre.length, b.pre.length); i++) {
    const [x, y] = [a.pre[i], b.pre[i]];
    if (x === undefined) return -1;
    if (y === undefined) return 1;
    const [nx, ny] = [/^\d+$/.test(x), /^\d+$/.test(y)];
    if (nx && ny && Number(x) !== Number(y)) return Number(x) - Number(y);
    if (nx !== ny) return nx ? -1 : 1;
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}

/**
 * The release-branch build of package version `pkg` in run `run` (attempt `attempt`) at `sha`.
 * @param {{ pkg: string, run: string, attempt: string, sha: string }} build
 */
export function identity({ pkg, run, attempt, sha }) {
  const parsed = parseVersion(pkg);
  if (!parsed || parsed.build) throw new Error(`app/package.json version ${pkg} is not a plain semver version`);
  if (!/^\d+$/.test(run) || !/^\d+$/.test(attempt)) throw new Error(`bad run ${run} / attempt ${attempt}`);
  if (!/^[0-9a-f]{40}$/i.test(sha)) throw new Error(`bad commit ${sha}`);
  const short = sha.slice(0, 12).toLowerCase();
  return {
    version: `${pkg}+release.${run}.${short}`,
    name: `${pkg}-release.${run}.${short}`,
    path: `release/${run}.${attempt}-${short}`,
  };
}

/**
 * Whether `ancestor` is `commit` or an ancestor of it in the checkout `cwd`.
 * @param {string} ancestor @param {string} commit @param {string} [cwd]
 */
function isAncestor(ancestor, commit, cwd) {
  return spawnSync("git", ["merge-base", "--is-ancestor", ancestor, commit], { cwd }).status === 0;
}

/**
 * Every item of a paginated GitHub list (`key` names the array), following Link: rel="next".
 * @param {string} url @param {string} key @param {string} token
 */
async function list(url, key, token) {
  /** @type {any[]} */
  const items = [];
  /** @type {string | null} */
  let next = url;
  while (next) {
    const res = await fetch(next, {
      headers: { Accept: "application/vnd.github+json", Authorization: `Bearer ${token}`, "X-GitHub-Api-Version": "2022-11-28" },
    });
    if (!res.ok) throw new Error(`GET ${next}: ${res.status}`);
    /** @type {any} */
    const page = await res.json();
    items.push(...(page[key] ?? []));
    next = /<([^>]+)>;\s*rel="next"/.exec(res.headers.get("link") ?? "")?.[1] ?? null;
  }
  return items;
}

/**
 * Why the commit `sha` may not be built, or null when it may: the newest
 * run of each REQUIRED workflow for exactly that commit, on main (pushed or
 * dispatched there), must be completed and successful.
 * @param {{ path: string, runs: any[] }[]} workflows
 * @param {string} sha
 */
export function ciRefusal(workflows, sha) {
  for (const path of REQUIRED) {
    const workflow = workflows.find((w) => w.path === path);
    if (!workflow) return `${path} is not an active workflow`;
    const runs = workflow.runs
      .filter((r) => r.head_sha === sha && r.head_branch === "main" && ["push", "workflow_dispatch"].includes(r.event))
      .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id - a.id);
    const newest = runs[0];
    if (!newest) return `${path} has no run on main for ${sha}`;
    const attempt = `${path} run ${newest.id} (attempt ${newest.run_attempt})`;
    if (newest.status !== "completed") return `${attempt} is ${newest.status}`;
    if (newest.conclusion !== "success") return `${attempt} ended ${newest.conclusion}`;
  }
  return null;
}

/**
 * Whether the new manifest `next` may replace the published `current` (null: none yet).
 * @param {{ version: string } | null} current @param {{ version: string }} next
 * @param {(ancestor: string, commit: string) => boolean} descends
 * @returns {{ go: boolean, reason: string }}
 */
export function promotion(current, next, descends) {
  const to = parseVersion(next.version);
  if (!to || to.run === null || !to.sha) throw new Error(`${next.version} is not a release-branch build`);
  if (!current) return { go: true, reason: "no release build published yet" };
  const from = parseVersion(current.version);
  if (!from || from.run === null || !from.sha) throw new Error(`the published ${current.version} is not a release-branch build`);
  if (to.run <= from.run) return { go: false, reason: `run ${to.run} is not later than the published run ${from.run}` };
  if (precedence(to, from) < 0) return { go: false, reason: `${next.version} is older than the published ${current.version}` };
  if (!descends(from.sha, to.sha)) return { go: false, reason: `${to.sha} does not descend from the published ${from.sha}` };
  return { go: true, reason: `${current.version} -> ${next.version}` };
}

/**
 * Check both Mac builds in `dir` against the identity and write latest.json and build.json into dir/<name>.
 * @param {string} dir
 * @param {{ version: string, name: string, path: string, site: string, sha: string, runId: string, attempt: string }} build
 */
export function verify(dir, { version, name, path, site, sha, runId, attempt }) {
  // A re-run of the publish job alone keeps the gate's outputs from an earlier
  // attempt: publishing there would overwrite that attempt's immutable files.
  const parsed = parseVersion(version);
  if (!parsed || path !== `release/${parsed.run}.${attempt}-${parsed.sha}`) {
    throw new Error(`${path} is not this attempt's (${attempt}) directory`);
  }
  const files = join(dir, name);
  const base = `${site}/${path}`;
  /** @type {Record<string, { signature: string, url: string }>} */
  const platforms = {};
  for (const arch of ARCHES) {
    const statusFile = join(dir, `macos-${arch}.status.json`);
    if (!existsSync(statusFile)) throw new Error(`no ${arch} build`);
    const status = JSON.parse(readFileSync(statusFile, "utf8"));
    for (const field of ["version", "plist_version", "app_version"]) {
      if (status[field] !== version) throw new Error(`the ${arch} build's ${field} is ${status[field]}, not ${version}`);
    }
    for (const flag of ["signed", "notarized", "updater"]) {
      if (status[flag] !== true) throw new Error(`the ${arch} build is not ${flag}`);
    }
    const stem = `brindle_${name}_${arch}`;
    for (const file of [`${stem}.dmg`, `${stem}.app.tar.gz`, `${stem}.app.tar.gz.sig`]) {
      if (!existsSync(join(files, file))) throw new Error(`no ${file}`);
    }
    platforms[`darwin-${arch}`] = {
      signature: readFileSync(join(files, `${stem}.app.tar.gz.sig`), "utf8").trim(),
      url: `${base}/${stem}.app.tar.gz`,
    };
  }
  const manifest = {
    version,
    notes: `brindle desktop ${version} (release/desktop ${sha})`,
    pub_date: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
    platforms,
  };
  writeFileSync(join(files, "latest.json"), `${JSON.stringify(manifest, null, 2)}\n`);
  const build = { version, package_version: version.split("+")[0], commit: sha, run_id: runId, run_attempt: attempt, path };
  writeFileSync(join(files, "build.json"), `${JSON.stringify(build, null, 2)}\n`);
  for (const arch of ARCHES) rmSync(join(dir, `macos-${arch}.status.json`));
  return manifest;
}

/** @param {Record<string, string | boolean>} values */
function output(values) {
  const lines = Object.entries(values).map(([k, v]) => `${k}=${v}`).join("\n");
  console.log(lines);
  if (process.env.GITHUB_OUTPUT) appendFileSync(process.env.GITHUB_OUTPUT, `${lines}\n`);
}

/** @param {string} name */
function need(name) {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is not set`);
  return value;
}

/** @param {string[]} args */
async function main(args) {
  const env = process.env;
  switch (args[0]) {
    case "ci-gate": {
      const sha = need("GITHUB_SHA");
      const repo = `${env.GITHUB_API_URL ?? "https://api.github.com"}/repos/${need("GITHUB_REPOSITORY")}`;
      const token = need("GITHUB_TOKEN");
      const mainRef = env.MAIN_REF ?? "origin/main";
      let refusal = isAncestor(sha, mainRef) ? null : `${sha} is not on ${mainRef}`;
      if (!refusal) {
        const active = (await list(`${repo}/actions/workflows?per_page=100`, "workflows", token)).filter((w) => w.state === "active");
        const workflows = [];
        for (const w of active.filter((w) => REQUIRED.includes(w.path))) {
          workflows.push({ path: w.path, runs: await list(`${repo}/actions/workflows/${w.id}/runs?head_sha=${sha}&per_page=100`, "workflow_runs", token) });
        }
        refusal = ciRefusal(workflows, sha);
      }
      if (refusal) {
        console.log(`::error::not building ${sha}: ${refusal}`);
        output({ go: false });
        return 1;
      }
      output({ go: true });
      return 0;
    }
    case "identity": {
      const pkg = JSON.parse(readFileSync(resolve(HERE, "../package.json"), "utf8")).version;
      output(identity({ pkg, run: need("GITHUB_RUN_NUMBER"), attempt: need("GITHUB_RUN_ATTEMPT"), sha: need("GITHUB_SHA") }));
      return 0;
    }
    case "verify": {
      const dir = args[1];
      verify(dir, {
        version: need("VERSION"), name: need("NAME"), path: need("DEST_PATH"), site: need("SITE_DOWNLOADS"),
        sha: need("GITHUB_SHA"), runId: need("GITHUB_RUN_ID"), attempt: need("GITHUB_RUN_ATTEMPT"),
      });
      output({ dir, signed: true, notarized: true });
      return 0;
    }
    case "promote": {
      const [currentFile, nextFile] = args.slice(1);
      const current = existsSync(currentFile) ? JSON.parse(readFileSync(currentFile, "utf8")) : null;
      const decision = promotion(current, JSON.parse(readFileSync(nextFile, "utf8")), (a, c) => isAncestor(a, c));
      if (!decision.go) console.log(`::warning::not moving release/latest.json: ${decision.reason}`);
      output({ go: decision.go });
      return 0;
    }
    case "oidc-token": {
      const url = `${need("ACTIONS_ID_TOKEN_REQUEST_URL")}&audience=sts.amazonaws.com`;
      for (let attempt = 1; ; attempt++) {
        const res = await fetch(url, { headers: { Authorization: `bearer ${need("ACTIONS_ID_TOKEN_REQUEST_TOKEN")}` } }).catch(() => null);
        /** @type {any} */
        const answer = res?.ok ? await res.json() : null;
        const token = answer?.value;
        if (token) {
          process.stdout.write(token);
          return 0;
        }
        if (attempt === 5) throw new Error("no OIDC token from GitHub (does the job have id-token: write?)");
        await new Promise((done) => setTimeout(done, attempt * 2000));
      }
    }
    default:
      throw new Error(`usage: release_publish.mjs ci-gate|identity|verify DIR|promote CURRENT NEXT|oidc-token`);
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).then(
    (code) => process.exit(code),
    (e) => {
      console.log(`::error::${e.message}`);
      process.exit(1);
    },
  );
}
