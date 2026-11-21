import { expect, it } from "vitest";
import { GroundContrast } from "./groundContrast";

const through = (...chunks: string[]) => {
  const lift = new GroundContrast(() => ({ theme: { background: "#ffffff" }, ratio: 4.5 }));
  return chunks.map(chunk => new TextDecoder().decode(lift.feed(new TextEncoder().encode(chunk)))).join("");
};

// A faint grey set on the field is lifted; once the program sets a ground (as Claude's diff sets each token's
// foreground, then its background), its own colour is put back for the cells drawn there, and lifted again after.
it("lifts a foreground only while it sits on the terminal's own ground", () => {
  const out = through("\x1b[38;5;246mfield\x1b[48;5;217mdiff\x1b[4", "9mfield");
  const [field, diff, back] = out.split(/field|diff/).slice(0, 3);
  expect(field).toMatch(/\x1b\[38;5;246m\x1b\[38;2;\d+;\d+;\d+m$/);
  expect(diff).toBe("\x1b[48;5;217m\x1b[38;5;246m");
  expect(back).toMatch(/^\x1b\[49m\x1b\[38;2;\d+;\d+;\d+m$/);
});
