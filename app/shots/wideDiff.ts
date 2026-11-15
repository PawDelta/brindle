/* A realistic session diff for the review scenes: about a dozen files across
   nested folders with long paths, a huge +/- count, a rename, a deletion,
   long code lines and several hunks per file. It hides nothing a real PR
   would show, so layout overlaps surface in the shots. */
import { unifiedDiff } from "../src/test/fakeDaemon";

const LONG = "const message = await client.postSessionReviewDecision({ sessionId, workspaceId, branch, base, files: changedFiles.map((f) => f.path), reason: 'a deliberately long argument list so the line runs well past any column' });";

/* `count` lines of code; those at a multiple of `every` differ between versions. */
const body = (tag: string, count: number, every: number, version: number): string =>
  Array.from({ length: count }, (_, i) => (i % every === 0 ? `  ${tag}_${i}_v${version}(${LONG.slice(0, 40 + ((i * 37) % 160))});` : `  const ${tag}_${i} = compute(${i}); // unchanged`)).join("\n") + "\n";

export interface WideFile { path: string; before: string | null; after: string | null; from?: string }

export const WIDE_FILES: WideFile[] = [
  { path: "src/brindle/daemon/session_review.py", before: body("review", 80, 9, 1), after: body("review", 90, 9, 2) },
  { path: "app/src/screens/Session/Visuals/SharedDesignReader.test.tsx", before: body("reader", 60, 7, 1), after: body("reader", 66, 7, 2) },
  { path: "app/src/screens/Session/SessionDiffEditor.tsx", before: body("editor", 120, 11, 1), after: body("editor", 120, 11, 2) },
  // +1234 −567: every line differs.
  { path: "packages/engine/src/brindle/native/generated/schema_registry_snapshot.json", before: Array.from({ length: 567 }, (_, i) => `old line ${i}\n`).join(""), after: Array.from({ length: 1234 }, (_, i) => `  "new_entry_${i}": ${LONG.slice(0, 30 + (i % 90))}\n`).join("") },
  { path: "src/brindle/daemon/routes_read.py", before: body("routes", 40, 5, 1), after: body("routes", 40, 5, 2) },
  { path: "docs/architecture/session-review-and-pull-request-flow-with-a-very-long-name.md", before: body("doc", 30, 4, 1), after: body("doc", 34, 4, 2) },
  { path: "app/src/screens/Session/sessionDiff.ts", before: body("diff", 50, 8, 1), after: body("diff", 50, 8, 2) },
  { path: "tests/daemon/test_session_review_pull_request_readiness_and_diff.py", before: null, after: body("test", 45, 3, 2) },
  { path: "app/src/legacy/OldSessionReviewSheetWithAnExtremelyLongComponentName.tsx", before: body("old", 25, 3, 1), after: null },
  { path: "app/src/screens/Session/review.ts", from: "app/src/screens/Session/reviewHelpers.ts", before: body("helpers", 35, 6, 1), after: body("helpers", 35, 6, 2) },
  { path: "README.md", before: body("readme", 12, 3, 1), after: body("readme", 12, 3, 2) },
  { path: "scripts/check_new_tests.py", before: body("check", 20, 4, 1), after: body("check", 22, 4, 2) },
];

/* The patch, as git prints it; a rename's headers are rewritten to git's rename form. */
export function wideDiff(): string {
  return WIDE_FILES.map((f) => {
    if (!f.from) return unifiedDiff(f.path, f.before, f.after);
    const patch = unifiedDiff(f.from, f.before, f.after);
    return patch.replace(`diff --git a/${f.from} b/${f.from}`, `diff --git a/${f.from} b/${f.path}\nsimilarity index 80%\nrename from ${f.from}\nrename to ${f.path}`).replace(`+++ b/${f.from}`, `+++ b/${f.path}`);
  }).join("");
}

export const wideCheckout = (): Record<string, string> => Object.fromEntries(WIDE_FILES.filter((f) => f.after !== null).map((f) => [f.path, f.after as string]));
