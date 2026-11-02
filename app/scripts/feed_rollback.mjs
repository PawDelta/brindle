#!/usr/bin/env node
/* feed_rollback.mjs — the stable feed's rollback manifest (app-feed-rollback.yml).

   `node feed_rollback.mjs VERSION SITE DIR` reads release VERSION's two
   published updater bundles and their signatures from DIR
   (brindle_<VERSION>_<arch>.app.tar.gz and .sig, as app-release.yml publishes
   them under SITE/<VERSION>/), checks each bundle's minisign signature against
   the production updater key (tauri.conf.json `plugins.updater.pubkey`, the
   one installed apps check with), and writes DIR/latest.json: Tauri's manifest
   naming those same bundles, marked `"rollback": true` with the notes
   "Rolled back to VERSION". Nothing is rebuilt or re-signed. The app offers
   an older release only from a manifest so marked (src-tauri/src/updates.rs
   `update_wanted`). It refuses a VERSION that isn't a plain X.Y.Z release, a
   missing file, or a signature that doesn't verify. */

import { createHash, createPublicKey, verify as verifySignature } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const ARCHES = ["aarch64", "x86_64"];
const STABLE = /^\d+\.\d+\.\d+$/;

/**
 * A minisign file's base64 line (the first not a comment) decoded.
 * @param {string} text
 */
function payload(text) {
  const line = text.split("\n").map((l) => l.trim()).find((l) => l && !l.includes(":"));
  if (!line) throw new Error("not a minisign file");
  return Buffer.from(line, "base64");
}

/** @param {Buffer} raw an Ed25519 public key's 32 bytes */
function ed25519(raw) {
  return createPublicKey({ key: { kty: "OKP", crv: "Ed25519", x: raw.toString("base64url") }, format: "jwk" });
}

/**
 * Throw unless `signature` (base64 of a minisign signature file, as a .sig and
 * a manifest hold it) signs `bytes` with `pubkey` (base64 of a minisign
 * public key file, as tauri.conf.json holds it), its trusted comment included.
 * @param {string} pubkey
 * @param {string} signature
 * @param {Buffer} bytes
 */
export function checkSignature(pubkey, signature, bytes) {
  const key = payload(Buffer.from(pubkey.trim(), "base64").toString("utf8"));
  const file = Buffer.from(signature.trim(), "base64").toString("utf8");
  const sig = payload(file);
  if (key.length !== 42 || key.subarray(0, 2).toString() !== "Ed") throw new Error("not a minisign Ed25519 public key");
  if (sig.length !== 74) throw new Error("not a minisign signature");
  if (!sig.subarray(2, 10).equals(key.subarray(2, 10))) throw new Error("signed with another key");
  const algorithm = sig.subarray(0, 2).toString();
  // "ED" signs the file's BLAKE2b-512 hash, "Ed" (legacy) the file itself.
  const signed = algorithm === "ED" ? createHash("blake2b512").update(bytes).digest() : algorithm === "Ed" ? bytes : null;
  if (!signed) throw new Error(`unknown signature algorithm ${algorithm}`);
  const pk = ed25519(key.subarray(10));
  if (!verifySignature(null, signed, pk, sig.subarray(10))) throw new Error("the signature does not match the bundle");
  // The trusted comment, then the global signature over the signature and that comment.
  const lines = file.split("\n").map((l) => l.replace(/\r$/, ""));
  const at = lines.findIndex((l) => l.startsWith("trusted comment: "));
  const global = at < 0 ? undefined : lines[at + 1]?.trim();
  if (!global) throw new Error("the signature has no trusted comment");
  const comment = Buffer.concat([sig.subarray(10), Buffer.from(lines[at].slice("trusted comment: ".length), "utf8")]);
  if (!verifySignature(null, comment, pk, Buffer.from(global, "base64"))) throw new Error("the signature's trusted comment does not verify");
}

/**
 * The rollback manifest for release `version`, from its bundles in `dir`,
 * each checked against `pubkey`.
 * @param {{ version: string, site: string, dir: string, pubkey: string, date?: Date }} rollback
 */
export function rollbackManifest({ version, site, dir, pubkey, date = new Date() }) {
  if (!STABLE.test(version)) throw new Error(`${JSON.stringify(version)} is not a stable release version (X.Y.Z)`);
  /** @type {Record<string, { signature: string, url: string }>} */
  const platforms = {};
  for (const arch of ARCHES) {
    const bundle = `brindle_${version}_${arch}.app.tar.gz`;
    for (const file of [bundle, `${bundle}.sig`]) {
      if (!existsSync(join(dir, file))) throw new Error(`no ${file}: is ${version} published?`);
    }
    const signature = readFileSync(join(dir, `${bundle}.sig`), "utf8").trim();
    try {
      checkSignature(pubkey, signature, readFileSync(join(dir, bundle)));
    } catch (e) {
      throw new Error(`${bundle}: ${e instanceof Error ? e.message : e}`);
    }
    platforms[`darwin-${arch}`] = { signature, url: `${site}/${version}/${bundle}` };
  }
  return {
    version,
    notes: `Rolled back to ${version}`,
    pub_date: date.toISOString().replace(/\.\d+Z$/, "Z"),
    rollback: true,
    platforms,
  };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const [version = "", site = "", dir = ""] = process.argv.slice(2);
  const conf = JSON.parse(readFileSync(new URL("../src-tauri/tauri.conf.json", import.meta.url), "utf8"));
  const manifest = rollbackManifest({ version, site, dir, pubkey: conf.plugins.updater.pubkey });
  const text = `${JSON.stringify(manifest, null, 2)}\n`;
  writeFileSync(join(dir, "latest.json"), text);
  process.stdout.write(text);
}
