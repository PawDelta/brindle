#!/usr/bin/env node
/* release_notes.mjs — the one-line summary app-release.yml puts in latest.json's `notes`.

   `node release_notes.mjs TAG FALLBACK` prints the summary of annotated tag TAG
   (its message's first paragraph, one line, at most 200 characters, cut on a
   word boundary with an ellipsis), or FALLBACK when TAG is not an annotated tag
   (a lightweight tag, a branch run, no such tag) or its message is empty. */
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

export const MAX = 200;

/** The first paragraph of `message` on one line, capped at MAX characters on a word boundary; "" if none. */
export function summarize(message, max = MAX) {
  const paragraph = (message ?? "").replace(/\r\n?/g, "\n").trim().split(/\n[ \t]*\n/)[0];
  const text = paragraph.replace(/\s+/g, " ").trim();
  if (text.length <= max) return text;
  const room = text.slice(0, max - 1);
  const cut = room.lastIndexOf(" ");
  return `${(cut > 0 ? room.slice(0, cut) : room).replace(/[\s.,;:]+$/, "")}…`;
}

/** The summary for `tag` in the current repository, or `fallback`. */
export function notesFor(tag, fallback, git = defaultGit) {
  if (!tag || git(["cat-file", "-t", `refs/tags/${tag}`]).trim() !== "tag") return fallback;
  // %(contents) of a tag object is its message (and a signature block, which the first paragraph precedes).
  const message = git(["for-each-ref", "--format=%(contents)", `refs/tags/${tag}`]);
  return summarize(message) || fallback;
}

function defaultGit(args) {
  const done = spawnSync("git", args, { encoding: "utf8" });
  return done.status === 0 ? done.stdout : "";
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const [tag, fallback = ""] = process.argv.slice(2);
  process.stdout.write(`${notesFor(tag, fallback)}\n`);
}
