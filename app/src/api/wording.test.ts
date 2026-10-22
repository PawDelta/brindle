import { describe, expect, it } from "vitest";
import { plain } from "./wording";

describe("plain", () => {
  it.each([
    ["cost reports need brindle Pro: `brindle account upgrade`", "cost reports need brindle Pro."],
    ["brindle 0.5.1 is available (you have 0.5.0): uv tool install --upgrade brindle", "brindle 0.5.1 is available (you have 0.5.0)."],
    ["Claude Code isn't signed in: run `claude auth login` (or start `claude` and log in), then try again", "Claude Code isn't signed in, then try again."],
    ["an accepted decision on this topic already covers these paths (abcdef12); pass supersedes to replace it", "an accepted decision on this topic already covers these paths (abcdef12); pass supersedes to replace it."],
    ["log in to brindle Pro again: run `brindle account login`", "log in to brindle Pro again."],
    ["", ""],
  ])("drops the typed command from %j", (text, expected) => {
    expect(plain(text)).toBe(expected);
  });
});
