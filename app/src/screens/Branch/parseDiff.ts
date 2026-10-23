/* The engine's diff text (brindle git.diff: `git diff --patch <merge-base>`,
   then a summary of untracked files) split into one entry per file, for the
   file list and the unified view of the one selected, plus that summary. */

export interface FileDiff {
  path: string;
  /* The path it was renamed from. */
  oldPath: string | null;
  status: "added" | "deleted" | "renamed" | "modified";
  added: number;
  removed: number;
  binary: boolean;
  /* The file's lines from its first hunk header on ("@@ ...", "+...", "-...", " ..."). */
  lines: string[];
  /* The new side's blob sha from the "index <old>..<new>" line (abbreviated,
     as git prints it): what an inline edit is pinned to. */
  blob: string | null;
}

/* One hunk of a file: its header line's index in `lines`, the lines after it
   up to the next header, and where its new side starts in the file. */
export interface Hunk {
  header: string;
  /* Index of the header in FileDiff.lines; the body follows up to `end` (exclusive). */
  start: number;
  end: number;
  /* The new side's first line number (1-based) and its length, from "+<start>,<count>". */
  newStart: number;
  newCount: number;
}

export function hunksOf(file: FileDiff): Hunk[] {
  const out: Hunk[] = [];
  file.lines.forEach((line, i) => {
    if (!line.startsWith("@@")) return;
    const m = line.match(/\+(\d+)(?:,(\d+))?/);
    out.push({ header: line, start: i, end: file.lines.length, newStart: m ? Number(m[1]) : 1, newCount: m ? (m[2] === undefined ? 1 : Number(m[2])) : 0 });
    if (out.length > 1) out[out.length - 2].end = i;
  });
  return out;
}

/* The hunk's new side: its context and added lines, without their sign. */
export const newSide = (file: FileDiff, hunk: Hunk): string[] =>
  file.lines.slice(hunk.start + 1, hunk.end).filter((l) => !l.startsWith("-") && !l.startsWith("\\")).map((l) => l.slice(1));

/* `content` with the hunk's new side replaced by `edited` (the file's lines
   from newStart for newCount lines). The file's final newline is kept. */
export function splice(content: string, hunk: Hunk, edited: string[]): string {
  const trailing = content.endsWith("\n");
  const lines = (trailing ? content.slice(0, -1) : content).split("\n");
  lines.splice(hunk.newStart - 1, hunk.newCount, ...edited);
  return lines.join("\n") + (trailing ? "\n" : "");
}

/* git.summarize_untracked: "Untracked files (N):" then one indented entry a
   line ("  notes.md", "  docs/ (3 files)", "  ... and 4 more"). */
export interface Untracked {
  count: number;
  entries: string[];
}

export interface ParsedDiff {
  files: FileDiff[];
  untracked: Untracked | null;
}

const UNTRACKED = /^Untracked files \((\d+)\):$/;

const ESCAPES: Record<string, number> = { a: 7, b: 8, t: 9, n: 10, v: 11, f: 12, r: 13, '"': 34, "\\": 92 };

/* A path as git prints it: as is, or C-quoted when it has a quote, a
   backslash, a control or a non-ASCII character ("caf\303\251.py", octal
   escapes being the UTF-8 bytes). Returns the path and the text after it. */
function readPath(text: string): [string, string] | null {
  if (!text.startsWith('"')) return null;
  const bytes: number[] = [];
  const encoder = new TextEncoder();
  for (let i = 1; i < text.length; i++) {
    const c = text[i];
    if (c === '"') return [new TextDecoder().decode(new Uint8Array(bytes)), text.slice(i + 1)];
    if (c !== "\\") {
      bytes.push(...encoder.encode(c));
      continue;
    }
    const next = text[++i];
    const octal = text.slice(i, i + 3);
    if (/^[0-7]{3}$/.test(octal)) {
      bytes.push(parseInt(octal, 8));
      i += 2;
    } else if (next in ESCAPES) {
      bytes.push(ESCAPES[next]);
    } else {
      return null;
    }
  }
  return null;
}

/* A path after its "a/" or "b/" prefix, quoted or not; null for /dev/null.
   An unquoted "---" / "+++" name with a space ends in a tab. */
function unprefixed(text: string): string | null {
  const quoted = readPath(text);
  const path = quoted ? quoted[0] : text.replace(/\t$/, "");
  return /^[ab]\//.test(path) ? path.slice(2) : null;
}

/* The new path from `diff --git a/<old> b/<new>`. Unquoted names can hold
   spaces, so an unquoted pair is split where the two halves agree; the
   "+++" and "rename to" lines, when they come, settle it anyway. */
function headerPath(rest: string): string {
  const old = readPath(rest);
  if (old) return unprefixed(old[1].trimStart()) ?? old[0];
  const quotedNew = rest.match(/ ("(?:[^"\\]|\\.)*")$/);
  if (quotedNew) return unprefixed(quotedNew[1]) ?? rest;
  const half = (rest.length - 1) / 2;
  if (Number.isInteger(half) && rest.slice(2, half) === rest.slice(half + 3)) return rest.slice(half + 3);
  return rest.match(/ b\/(.+)$/)?.[1] ?? rest;
}

/* A "rename from" / "rename to" name: no prefix, quoted when it needs to be. */
const plainPath = (text: string) => readPath(text)?.[0] ?? text;

export function parseDiff(text: string): ParsedDiff {
  const files: FileDiff[] = [];
  let untracked: Untracked | null = null;
  let file: FileDiff | null = null;
  let inHunks = false;
  for (const line of text.split("\n")) {
    if (untracked) {
      // ls-files C-quotes these too.
      if (line.startsWith("  ")) untracked.entries.push(plainPath(line.slice(2)));
      continue;
    }
    const summary = line.match(UNTRACKED);
    if (summary) {
      untracked = { count: Number(summary[1]), entries: [] };
      continue;
    }
    if (line.startsWith("diff --git ")) {
      const path = headerPath(line.slice("diff --git ".length));
      file = { path, oldPath: null, status: "modified", added: 0, removed: 0, binary: false, lines: [], blob: null };
      files.push(file);
      inHunks = false;
      continue;
    }
    if (!file) continue;
    if (line.startsWith("@@")) inHunks = true;
    if (inHunks) {
      if (line.startsWith("+")) file.added++;
      else if (line.startsWith("-")) file.removed++;
      file.lines.push(line);
      continue;
    }
    if (line.startsWith("index ")) file.blob = line.slice(6).split(" ")[0].split("..")[1] ?? null;
    else if (line.startsWith("new file")) file.status = "added";
    else if (line.startsWith("deleted file")) file.status = "deleted";
    else if (line.startsWith("rename from ")) {
      file.status = "renamed";
      file.oldPath = plainPath(line.slice("rename from ".length));
    } else if (line.startsWith("rename to ")) file.path = plainPath(line.slice("rename to ".length));
    else if (line.startsWith("+++ ")) file.path = unprefixed(line.slice(4)) ?? file.path;
    else if (line.startsWith("Binary files")) file.binary = true;
  }
  for (const f of files) while (f.lines.at(-1) === "") f.lines.pop();
  return { files, untracked };
}
