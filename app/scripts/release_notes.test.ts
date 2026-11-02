// @vitest-environment node
import { describe, expect, it } from "vitest";
import { notesFor, summarize } from "./release_notes.mjs";

describe("summarize", () => {
  it("takes the first paragraph on one line", () => {
    expect(summarize("  Faster\nstartup.  \n\nSecond paragraph.\n")).toBe("Faster startup.");
  });

  it("caps on a word boundary with an ellipsis", () => {
    const out = summarize(`${"word ".repeat(80)}end`);
    expect(out.length).toBeLessThanOrEqual(200);
    expect(out.endsWith("word…")).toBe(true);
  });

  it("cuts an unbroken run at the cap", () => {
    expect(summarize("x".repeat(500))).toBe(`${"x".repeat(199)}…`);
  });
});

describe("notesFor", () => {
  const push = { event: "push", refType: "tag", rc: "false", tag: "app-v1.2.3" };
  const fake = (type: string, message: string) => (args: string[]) => (args[0] === "cat-file" ? `${type}\n` : message);

  it("uses an annotated tag's message", () => {
    expect(notesFor(push, "fallback", fake("tag", "Adds the update banner.\n\nDetails."))).toBe("Adds the update banner.");
  });

  it.each([
    ["a lightweight tag", push, fake("commit", "Merge branch x")],
    ["an empty message", push, fake("tag", " \n\n ")],
    ["no such tag", push, () => ""],
    ["a manual run of an annotated tag", { ...push, event: "workflow_dispatch" }, fake("tag", "Adds x.")],
    ["a release candidate", { ...push, rc: "true" }, fake("tag", "Adds x.")],
    ["a branch run", { ...push, refType: "branch", tag: "main" }, fake("tag", "Adds x.")],
  ])("falls back for %s", (_, run, git) => {
    expect(notesFor(run, "fallback", git)).toBe("fallback");
  });
});
