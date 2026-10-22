// @vitest-environment node
import { spawnSync } from "node:child_process";
import { chmodSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const gate = fileURLToPath(new URL("./publish_gate.sh", import.meta.url));
const retry = fileURLToPath(new URL("./retry.sh", import.meta.url));
const notarizeDmg = fileURLToPath(new URL("./notarize_dmg.sh", import.meta.url));
const buildEngine = fileURLToPath(new URL("./build_engine.sh", import.meta.url));
const workflow = fileURLToPath(new URL("../../.github/workflows/app-release.yml", import.meta.url));
const engineWorkflow = (name: string) => fileURLToPath(new URL(`../../.github/workflows/${name}`, import.meta.url));
const prePush = fileURLToPath(new URL("../../.githooks/pre-push", import.meta.url));

const ready = { SIGNED: "true", NOTARIZED: "true", ROLE: "arn:aws:iam::1:role/r", BUCKET: "b" };

function decide(env: Record<string, string>): string {
  const { GITHUB_OUTPUT: _, ...inherited } = process.env;
  const run = spawnSync("bash", [gate], { env: { ...inherited, ...env }, encoding: "utf8" });
  expect(run.status).toBe(0);
  return run.stdout.trim().split("\n").at(-1) ?? "";
}

describe("app-release publish gate", () => {
  it.each([
    ["a pushed tag, signed, notarized, bucket set", { EVENT: "push", REF_TYPE: "tag", ...ready }, "go=true"],
    ["a manual run on a branch", { EVENT: "workflow_dispatch", REF_TYPE: "branch", ...ready }, "go=false"],
    ["a manual run on an existing tag", { EVENT: "workflow_dispatch", REF_TYPE: "tag", ...ready }, "go=false"],
    ["the nightly job", { EVENT: "schedule", REF_TYPE: "branch", ...ready }, "go=false"],
    ["a pushed tag, unsigned", { EVENT: "push", REF_TYPE: "tag", ...ready, SIGNED: "false" }, "go=false"],
    ["a pushed tag, not notarized", { EVENT: "push", REF_TYPE: "tag", ...ready, NOTARIZED: "false" }, "go=false"],
    ["a pushed tag, no role", { EVENT: "push", REF_TYPE: "tag", ...ready, ROLE: "" }, "go=false"],
    ["a pushed tag, no bucket", { EVENT: "push", REF_TYPE: "tag", ...ready, BUCKET: "" }, "go=false"],
    ["Linux: a pushed tag, nothing to sign", { PLATFORM: "linux", EVENT: "push", REF_TYPE: "tag", ROLE: "r", BUCKET: "b" }, "go=true"],
    ["Linux: a manual run on an existing tag", { PLATFORM: "linux", EVENT: "workflow_dispatch", REF_TYPE: "tag", ROLE: "r", BUCKET: "b" }, "go=false"],
  ])("%s: %s", (_case, env, expected) => {
    expect(decide(env)).toBe(expected);
  });

  it("guards every publishing step of both publishing jobs with the push event and the gate, and feeds each gate the event", () => {
    const text = readFileSync(workflow, "utf8");
    const lines = text.split("\n");
    const guard = "if: github.event_name == 'push' && steps.publish.outputs.go == 'true'";
    const steps = lines.flatMap((line, at) =>
      /^- name: (AWS credentials|Publish .*to pawdelta\.com\/brindle\/downloads\/desktop\/)$/.test(line.trim()) ? [at] : [],
    );
    expect(steps).toHaveLength(4);
    for (const at of steps) expect(lines[at + 1]?.trim(), lines[at]).toBe(guard);
    expect(text.split("EVENT: ${{ github.event_name }}")).toHaveLength(3);
    expect(text.split("run: bash app/scripts/publish_gate.sh")).toHaveLength(3);
  });

  it("builds each Mac architecture on its own runner and publishes only after both, with one bundle each in latest.json", () => {
    const text = readFileSync(workflow, "utf8");
    expect(text).not.toContain("universal-apple-darwin");
    expect(text).toMatch(/target: aarch64-apple-darwin\n\s+runner: macos-latest\n/);
    expect(text).toMatch(/target: x86_64-apple-darwin\n\s+runner: macos-15-intel\n/);
    const publish = text.slice(text.indexOf("\n  macos-publish:\n"), text.indexOf("\n  linux:\n"));
    expect(publish).toContain("    needs: macos\n");
    expect(publish).toContain('"darwin-aarch64": {signature: $arm_sig, url: $arm_url}');
    expect(publish).toContain('"darwin-x86_64": {signature: $x64_sig, url: $x64_url}');
    const build = text.slice(text.indexOf("\n  macos:\n"), text.indexOf("\n  macos-publish:\n"));
    expect(build).not.toContain("publish_gate.sh");
    expect(build).toContain('run: bash app/scripts/notarize_dmg.sh "$DMG"');
    expect(build).toContain("assess app -t exec \"$APP\"");
    expect(build).toContain("assess DMG -t open --context context:primary-signature \"$DMG\"");
  });

  it("keeps the secrets' jobs in the desktop-release environment, the schedule on the nightly job alone, and a release candidate off latest.json", () => {
    const text = readFileSync(workflow, "utf8");
    expect(text).toMatch(/^\s+schedule:/m);
    const env = "environment: ${{ github.event_name == 'push' && 'desktop-release' || '' }}";
    const job = (name: string) => {
      const start = text.indexOf(`\n  ${name}:\n`);
      return text.slice(start + 1, text.indexOf("\n  ", text.indexOf("steps:", start)));
    };
    for (const name of ["macos", "macos-publish", "linux"]) expect(job(name), name).toContain(env);
    // A scheduled run builds no release (and so signs and publishes nothing): only the nightly smoke runs.
    const release = "if: github.event_name == 'push' || (github.event_name == 'workflow_dispatch' && !inputs.nightly)";
    for (const name of ["macos", "macos-publish", "linux", "windows"]) expect(job(name), name).toContain(release);
    expect(job("nightly")).toContain("if: github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && inputs.nightly)");
    expect(text).toContain('[ "$REF_NAME" != "app-v$version" ]');
    const channel = text.match(/^\s+(case "\$version" in .*esac)$/m)?.[1] ?? "";
    expect(channel).not.toBe("");
    const pathOf = (version: string) =>
      spawnSync("bash", ["-c", `version=${version}; ${channel}; echo "$rc $path"`], { encoding: "utf8" }).stdout.trim();
    expect(pathOf("0.2.0")).toBe("false 0.2.0");
    expect(pathOf("0.2.0-rc.1")).toBe("true rc/0.2.0-rc.1");
    // A release candidate's manifest stays in its own directory, and its publish stops before latest.json.
    expect(text).toContain('[ "$RC" != true ] || manifest="$dir/$VERSION/latest.json"');
    const publish = text.slice(text.indexOf("- name: Publish to pawdelta.com/brindle/downloads/desktop/"));
    expect(publish.indexOf('if [ "$RC" = true ]; then')).toBeLessThan(publish.indexOf('"$dest/latest.json"'));
    // The rc channel's feed: written once, inside the release candidate's branch, before it stops.
    const rcBranch = publish.slice(publish.indexOf('if [ "$RC" = true ]; then'), publish.indexOf("exit 0"));
    expect(rcBranch).toContain('aws s3 cp "$DIR/$VERSION/latest.json" "$dest/rc/latest.json"');
    expect(text.split('"$dest/rc/latest.json"').length).toBe(2);
  });

  it("keeps app tags away from the engine's PyPI release and app changes out of the engine's CI", () => {
    // No GitHub Release for an app tag, and publish.yml (on release: published) skips any tag but v*.
    expect(readFileSync(workflow, "utf8")).not.toMatch(/contents: write|gh release/);
    const publish = readFileSync(engineWorkflow("publish.yml"), "utf8");
    const jobs = publish.slice(publish.indexOf("\njobs:\n")).split("\n").filter((line) => /^ {2}[a-z-]+:$/.test(line));
    const guards = publish.split("    if: startsWith(github.event.release.tag_name, 'v')\n").length - 1;
    expect(jobs.length).toBeGreaterThan(0);
    expect(guards).toBe(jobs.length);
    const ci = readFileSync(engineWorkflow("ci.yml"), "utf8");
    expect(ci).toMatch(/paths-ignore: \[[^\]]*"app\/\*\*"/);
    expect(ci).toMatch(/\*\.md\|docs\/\*\|[^)]*\|app\/\*\) ;;/);
  });

  it("has the versioned pre-push hook refuse an app-v* tag unless BRINDLE_RELEASE_TAG_OK=1", () => {
    const sha = "a".repeat(40);
    const push = (ref: string, env: Record<string, string>) =>
      spawnSync("bash", [prePush], {
        input: `${ref} ${sha} ${ref} ${"0".repeat(40)}\n`,
        env: { ...process.env, BRINDLE_RELEASE_TAG_OK: "", SKIP_LOCAL_CI: "1", ...env },
        encoding: "utf8",
      });
    expect(push("refs/tags/app-v0.2.0-rc.1", {}).status).toBe(1);
    expect(push("refs/tags/app-v0.2.0-rc.1", { BRINDLE_RELEASE_TAG_OK: "1" }).status).toBe(0);
    expect(push("refs/tags/v0.4.5", {}).status).toBe(0);
  });
});

describe("app-release retries (app/docs/release-failures.md)", () => {
  /** retry.sh running a command that fails until its `succeedAt`th run (never, when 0) with `code`. */
  function retried(args: string[], succeedAt: number, code = 7) {
    const dir = mkdtempSync(join(tmpdir(), "retry-"));
    const count = join(dir, "count");
    const cmd = `n=$(( $(cat '${count}' 2>/dev/null || echo 0) + 1 )); echo $n > '${count}'; [ ${succeedAt} != 0 ] && [ $n -ge ${succeedAt} ] || exit ${code}`;
    const run = spawnSync("bash", [retry, ...args, "-d", "0", "bash", "-c", cmd], { encoding: "utf8" });
    return { status: run.status, runs: Number(readFileSync(count, "utf8")), out: run.stdout };
  }

  it("retry.sh runs a failing command again until it succeeds, and fails as its last try did", () => {
    expect(retried(["-n", "3"], 1)).toMatchObject({ status: 0, runs: 1, out: "" });
    const flaky = retried(["-n", "3"], 3);
    expect(flaky).toMatchObject({ status: 0, runs: 3 });
    expect(flaky.out).toContain("::warning::bash failed (exit 7, try 1 of 3)");
    const broken = retried(["-n", "2"], 0, 68);
    expect(broken).toMatchObject({ status: 68, runs: 2 });
    expect(broken.out).toContain("::error::bash failed 2 times (last exit 68)");
  });

  /**
   * notarize_dmg.sh against a fake xcrun: the first `submitFails` uploads and `stapleFails` staples fail as a
   * dropped connection does, and each `notarytool wait` answers the next of `waits` ("none": no answer at all).
   */
  function notarize(opts: { submitFails?: number; waits?: string[]; stapleFails?: number; env?: Record<string, string> }) {
    const fake = mkdtempSync(join(tmpdir(), "notarize-"));
    const script = (name: string, body: string) => {
      writeFileSync(join(fake, name), `#!/usr/bin/env bash\n${body}\n`);
      chmodSync(join(fake, name), 0o755);
    };
    script("codesign", "exit 0");
    script(
      "xcrun",
      `echo "$*" >> "$FAKE/calls"
count() { local n; n=$(( $(cat "$FAKE/$1" 2>/dev/null || echo 0) + 1 )); echo $n > "$FAKE/$1"; echo $n; }
case "$1 $2" in
  "notarytool submit") [ "$(count submit)" -gt "$SUBMIT_FAILS" ] || { echo "The network connection was lost." >&2; exit 1; }
    echo '{"id": "sub-1", "message": "Successfully uploaded file"}' ;;
  "notarytool wait") s="$(echo "$WAITS" | cut -d, -f"$(count wait)")"
    [ "$s" != none ] || { echo "The network connection was lost." >&2; exit 1; }
    echo "{\\"id\\": \\"$3\\", \\"status\\": \\"$s\\"}" ;;
  "notarytool log") echo "the log of $3" ;;
  "stapler staple") [ "$(count staple)" -gt "$STAPLE_FAILS" ] || { echo "The staple and validate action failed! Error 68." >&2; exit 68; } ;;
esac`,
    );
    const env = {
      ...process.env,
      PATH: `${fake}:${process.env.PATH}`,
      FAKE: fake,
      SUBMIT_FAILS: String(opts.submitFails ?? 0),
      WAITS: (opts.waits ?? ["Accepted"]).join(","),
      STAPLE_FAILS: String(opts.stapleFails ?? 0),
      NOTARY_PAUSE: "0",
      APPLE_API_KEY_PATH: "/k/AuthKey_K.p8",
      APPLE_API_KEY: "K",
      APPLE_API_ISSUER: "I",
      ...opts.env,
    };
    const run = spawnSync("bash", [notarizeDmg, "/out/brindle_0.1.0_x86_64.dmg"], { env, encoding: "utf8" });
    let calls: string[] = [];
    try {
      calls = readFileSync(join(fake, "calls"), "utf8").trim().split("\n");
    } catch {}
    const of = (what: string) => calls.filter((c) => c.startsWith(what));
    return { status: run.status, out: run.stdout, calls, of };
  }

  it("notarizes and staples the DMG through dropped connections at every call to Apple", () => {
    const run = notarize({ submitFails: 2, waits: ["none", "In Progress", "Accepted"], stapleFails: 3 });
    expect(run.status, run.out).toBe(0);
    expect(run.of("notarytool submit")).toHaveLength(3);
    expect(run.of("notarytool wait")).toHaveLength(3);
    expect(run.of("notarytool wait sub-1 ")).toHaveLength(3);
    expect(run.of("stapler staple /out/brindle_0.1.0_x86_64.dmg")).toHaveLength(4);
    expect(run.of("stapler validate /out/brindle_0.1.0_x86_64.dmg")).toHaveLength(1);
    expect(run.of("notarytool submit")[0]).toContain("--key /k/AuthKey_K.p8 --key-id K --issuer I");
  });

  it("fails at once, with Apple's log, when Apple rejects the DMG", () => {
    const run = notarize({ waits: ["Invalid"] });
    expect(run.status).toBe(1);
    expect(run.out).toContain("::error::notarization of brindle_0.1.0_x86_64.dmg ended 'Invalid'");
    expect(run.out).toContain("the log of sub-1");
    expect(run.of("notarytool wait")).toHaveLength(1);
    expect(run.of("stapler")).toHaveLength(0);
  });

  it("gives up after its tries when Apple never answers, and staples nothing", () => {
    const submit = notarize({ submitFails: 99, env: { NOTARY_TRIES: "4" } });
    expect(submit.status).toBe(1);
    expect(submit.of("notarytool submit")).toHaveLength(4);
    expect(submit.of("notarytool wait")).toHaveLength(0);
    const wait = notarize({ waits: ["none", "none", "none", "none", "none"] });
    expect(wait.status).toBe(1);
    expect(wait.of("notarytool wait")).toHaveLength(5);
    expect(wait.of("stapler")).toHaveLength(0);
    const staple = notarize({ stapleFails: 99 });
    expect(staple.status).toBe(1);
    expect(staple.of("stapler staple")).toHaveLength(5);
    // An Apple ID instead of an API key.
    const id = notarize({ env: { APPLE_API_KEY_PATH: "", APPLE_ID: "a@b", APPLE_PASSWORD: "p", APPLE_TEAM_ID: "T" } });
    expect(id.status).toBe(0);
    expect(id.of("notarytool submit")[0]).toContain("--apple-id a@b --password p --team-id T");
  });

  it("puts every download and every call to Apple in the workflow through a retry", () => {
    const text = readFileSync(workflow, "utf8");
    const lines = text.split("\n");
    const network = /rustup (toolchain install|target add)|npm --prefix app (ci|run tauri)|npx tauri build|apt-get (update|install)|brew install|uv tool install/;
    const found = lines.filter((line) => network.test(line) && !line.trim().startsWith("#"));
    expect(found.length).toBeGreaterThanOrEqual(12);
    for (const line of found) expect(line, line.trim()).toMatch(/bash (app\/)?scripts\/retry\.sh /);
    // Apple: notarytool only through notarize_dmg.sh, each stapler call and spctl in a retry loop.
    expect(lines.filter((line) => line.includes("notarytool") && !line.trim().startsWith("#"))).toEqual([]);
    const step = (name: string) => text.slice(text.indexOf(`- name: ${name}\n`), text.indexOf("\n      - ", text.indexOf(`- name: ${name}\n`) + 1));
    expect(step("Staple the app")).toMatch(/for attempt in [^;]+; do\n\s+if xcrun stapler validate/);
    expect(step("Gatekeeper")).toMatch(/for attempt in 1 2 3; do\n\s+out="\$\(spctl -a -vvv/);
    expect(step("Gatekeeper")).toMatch(/for attempt in 1 2 3 4 5; do\n\s+if xcrun stapler validate "\$APP"/);
    for (const curl of lines.filter((line) => /\bcurl /.test(line))) expect(curl).toContain("--retry 5 --retry-all-errors");
    expect(text).toContain("AWS_RETRY_MODE: adaptive");
    // The engine's downloads and its timestamped signatures.
    const engine = readFileSync(buildEngine, "utf8");
    expect(engine).toContain('curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors -o "$file.part" "$url"');
    expect(engine).toMatch(/for attempt in 1 2 3 4 5; do\n\s+out="\$\(codesign --force --options runtime "\$ts"/);
  });
});
