// @vitest-environment node
import { createHash, generateKeyPairSync, sign, type KeyObject } from "node:crypto";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { rollbackManifest } from "./feed_rollback.mjs";

const b64 = (bytes: Buffer | string) => Buffer.from(bytes).toString("base64");

/* A minisign key as Tauri holds it: the public key file, base64 (tauri.conf.json's pubkey). */
function minisignKey(id: number) {
  const { publicKey, privateKey } = generateKeyPairSync("ed25519");
  const raw = Buffer.from(publicKey.export({ format: "jwk" }).x!, "base64url");
  const keyId = Buffer.alloc(8, id);
  const pubkey = b64(`untrusted comment: minisign public key\n${b64(Buffer.concat([Buffer.from("Ed"), keyId, raw]))}\n`);
  return { pubkey, keyId, privateKey };
}

/* A prehashed ("ED") minisign signature of `bytes`, base64 as a .sig holds it. */
function minisignSig(key: { keyId: Buffer; privateKey: KeyObject }, bytes: Buffer): string {
  const sig = sign(null, createHash("blake2b512").update(bytes).digest(), key.privateKey);
  const trusted = "timestamp:1\tfile:bundle.app.tar.gz";
  const global = sign(null, Buffer.concat([sig, Buffer.from(trusted)]), key.privateKey);
  const line = b64(Buffer.concat([Buffer.from("ED"), key.keyId, sig]));
  return b64(`untrusted comment: signature from tauri secret key\n${line}\ntrusted comment: ${trusted}\n${b64(global)}\n`);
}

const SITE = "https://pawdelta.com/brindle/downloads/desktop";

/* Release `version` as published: both updater bundles and their .sig, signed by `signer`. */
function published(version: string, signer: ReturnType<typeof minisignKey>, tamper = false): string {
  const dir = mkdtempSync(join(tmpdir(), "feed-rollback-"));
  for (const arch of ["aarch64", "x86_64"]) {
    const bundle = Buffer.from(`bundle ${arch}`);
    writeFileSync(join(dir, `brindle_${version}_${arch}.app.tar.gz`), tamper ? Buffer.from("tampered") : bundle);
    writeFileSync(join(dir, `brindle_${version}_${arch}.app.tar.gz.sig`), minisignSig(signer, bundle));
  }
  return dir;
}

describe("rollbackManifest", () => {
  const production = minisignKey(1);

  it("names the release's own signed bundles, marked a rollback", () => {
    const dir = published("0.2.9", production);
    const manifest = rollbackManifest({ version: "0.2.9", site: SITE, dir, pubkey: production.pubkey, date: new Date("2026-11-02T10:00:00.123Z") });
    expect(manifest).toEqual({
      version: "0.2.9",
      notes: "Rolled back to 0.2.9",
      pub_date: "2026-11-02T10:00:00Z",
      rollback: true,
      platforms: Object.fromEntries(
        ["aarch64", "x86_64"].map((arch) => [
          `darwin-${arch}`,
          { signature: minisignSig(production, Buffer.from(`bundle ${arch}`)), url: `${SITE}/0.2.9/brindle_0.2.9_${arch}.app.tar.gz` },
        ]),
      ),
    });
  });

  it.each([
    ["a bundle that doesn't match its signature", "0.2.9", () => published("0.2.9", production, true), /does not match/],
    ["a bundle signed with another key", "0.2.9", () => published("0.2.9", minisignKey(2)), /another key/],
    ["a release not published", "0.2.9", () => published("0.2.8", production), /no brindle_0\.2\.9_aarch64/],
    ["a release candidate", "0.3.0-rc.1", () => published("0.3.0-rc.1", production), /not a stable release/],
  ])("refuses %s", (_, version, dir, error) => {
    expect(() => rollbackManifest({ version, site: SITE, dir: dir(), pubkey: production.pubkey })).toThrow(error);
  });
});
