#!/usr/bin/env node
/* release_notes.mjs — the one-line summary app-release.yml puts in latest.json's `notes`.

   `node release_notes.mjs EVENT REF_TYPE RC REF_NAME FALLBACK` prints the summary
   of annotated tag REF_NAME (its message's first paragraph, one line, at most 200
   characters, cut on a word boundary with an ellipsis), or FALLBACK unless the run
   is a tag push of a final release (not a manual run, not a release candidate)
   whose tag is annotated with a non-empty message. */
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

export const MAX = 200;

/**
 * The first paragraph of `message` on one line, capped at `max` characters on a word boundary; "" if none.
 * @param {string | null | undefined} message
 * @param {number} [max]
 */
export function summarize(message, max = MAX) {
  const paragraph = (message ?? "").replace(/\r\n?/g, "\n").trim().split(/\n[ \t]*\n/)[0];
  const text = paragraph.replace(/\s+/g, " ").trim();
  if (text.length <= max) return text;
  const room = text.slice(0, max - 1);
  const cut = room.lastIndexOf(" ");
  return `${(cut > 0 ? room.slice(0, cut) : room).replace(/[\s.,;:]+$/, "")}…`;
}

/**
 * @param {string[]} args
 * @returns {string}
 */
function defaultGit(args) {
  const done = spawnSync("git", args, { encoding: "utf8" });
  return done.status === 0 ? done.stdout : "";
}

/**
 * The summary for the run, or `fallback`.
 * @param {{ event: string, refType: string, rc: string, tag: string }} run
 * @param {string} fallback
 * @param {(args: string[]) => string} [git]
 */
export function notesFor({ event, refType, rc, tag }, fallback, git = defaultGit) {
  if (event !== "push" || refType !== "tag" || rc === "true" || !tag) return fallback;
  if (git(["cat-file", "-t", `refs/tags/${tag}`]).trim() !== "tag") return fallback;
  // %(contents) of a tag object is its message (and a signature block, which the first paragraph precedes).
  return summarize(git(["for-each-ref", "--format=%(contents)", `refs/tags/${tag}`])) || fallback;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const [event = "", refType = "", rc = "", tag = "", fallback = ""] = process.argv.slice(2);
  process.stdout.write(`${notesFor({ event, refType, rc, tag }, fallback)}\n`);
}
