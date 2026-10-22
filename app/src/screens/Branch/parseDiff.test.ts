import { describe, expect, it } from "vitest";
import { hunksOf, newSide, parseDiff, splice } from "./parseDiff";

describe("parseDiff", () => {
  it("splits a file into hunks with their new side's place, and splices an edited new side back into the file", () => {
    const { files } = parseDiff("diff --git a/a.py b/a.py\nindex 1111111..abcdef0 100644\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n-x = 1\n+x = 2\n y = 3\n@@ -8 +8,2 @@\n z\n+w\n");
    expect(files[0].blob).toBe("abcdef0");
    const hunks = hunksOf(files[0]);
    expect(hunks.map((h) => [h.header, h.start, h.end, h.newStart, h.newCount])).toEqual([
      ["@@ -1,2 +1,2 @@", 0, 4, 1, 2],
      ["@@ -8 +8,2 @@", 4, 7, 8, 2],
    ]);
    expect(newSide(files[0], hunks[0])).toEqual(["x = 2", "y = 3"]);
    const content = "x = 2\ny = 3\n3\n4\n5\n6\n7\nz\nw\n";
    expect(splice(content, hunks[1], ["z", "w", "v"])).toBe("x = 2\ny = 3\n3\n4\n5\n6\n7\nz\nw\nv\n");
    expect(splice(content, hunks[0], ["x = 9"])).toBe("x = 9\n3\n4\n5\n6\n7\nz\nw\n");
  });

  it("reads git's C-quoted names (tabs, quotes, UTF-8 octal), renames and unquoted names with spaces", () => {
    const text = [
      "diff --git a/my notes.txt b/my notes.txt",
      "--- a/my notes.txt\t",
      "+++ b/my notes.txt\t",
      "@@ -1 +1 @@",
      "-a",
      "+b",
      'diff --git "a/tab\\there.txt" "b/tab\\there.txt"',
      "deleted file mode 100644",
      '--- "a/tab\\there.txt"\t',
      "+++ /dev/null",
      "@@ -1 +0,0 @@",
      "-gone",
      'diff --git a/old.md "b/na\\303\\257ve \\"draft\\".md"',
      "similarity index 100%",
      "rename from old.md",
      'rename to "na\\303\\257ve \\"draft\\".md"',
    ].join("\n");
    const { files, untracked } = parseDiff(text);
    expect(files.map((f) => [f.path, f.oldPath, f.status, f.added, f.removed])).toEqual([
      ["my notes.txt", null, "modified", 1, 1],
      ["tab\there.txt", null, "deleted", 0, 1],
      ['naïve "draft".md', "old.md", "renamed", 0, 0],
    ]);
    expect(untracked).toBeNull();
  });

  it("ends the patch at the untracked summary, which is read on its own", () => {
    const { files, untracked } = parseDiff(
      "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n\nUntracked files (25):\n  notes.md\n  .venv/ (24 files)",
    );
    expect(files).toHaveLength(1);
    expect(files[0].lines).toEqual(["@@ -1 +1 @@", "-x", "+y"]);
    expect(untracked).toEqual({ count: 25, entries: ["notes.md", ".venv/ (24 files)"] });
  });
});
