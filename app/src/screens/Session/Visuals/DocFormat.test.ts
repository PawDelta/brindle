import { expect, it } from "vitest";
import { formatEdit } from "./DocFormat";

it("starts an empty line with a line format's prefix and puts the caret after it", () => {
  const text = "Intro\n";
  const e = formatEdit(text, text.length, text.length, "ul");
  expect(text.slice(0, e.from) + e.insert + text.slice(e.to)).toBe("Intro\n- ");
  expect([e.anchor, e.head]).toEqual([8, 8]);
});
