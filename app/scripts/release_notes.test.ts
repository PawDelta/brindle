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

  it("is empty for nothing", () => {
    expect(summarize("  \n\n ")).toBe("");
  });
});

describe("notesFor", () => {
  const fake = (type: string, message: string) => (args: string[]) => (args[0] === "cat-file" ? `${type}\n` : message);

  it("uses an annotated tag's message", () => {
    expect(notesFor("app-v1.2.3", "fallback", fake("tag", "Adds the update banner.\n\nDetails."))).toBe("Adds the update banner.");
  });

  it.each([
    ["a lightweight tag", fake("commit", "Merge branch x")],
    ["an empty message", fake("tag", "\n")],
    ["no such tag", () => ""],
  ])("falls back for %s", (_, git) => {
    expect(notesFor("app-v1.2.3", "fallback", git)).toBe("fallback");
  });

  it("falls back with no tag (dispatch)", () => {
    expect(notesFor("", "fallback", fake("tag", "x"))).toBe("fallback");
  });
});
