// @vitest-environment node
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { button, card, chip, main, panel, pill } from "./components/ui";

/* The app wears the site's Crisp tokens (web/public/assets/site.css): the
   values, the radii and the braid's own threads in the logo, worn quiet:
   one plane for ground and surface, neutral hairlines, green only for the
   one main action, sections as dividers and pills as a dot and text. */
const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
const block = (selector: string) => {
  const at = css.indexOf(`${selector} {`);
  return css.slice(at, css.indexOf("}", at));
};
const token = (body: string, name: string) => body.match(new RegExp(`--${name}:\\s*([^;]+);`))?.[1].trim();

describe("the Crisp palette", () => {
  it("has the site's dark tokens", () => {
    const dark = block(":root");
    expect(token(dark, "canvas")).toBe("#080d0b");
    expect(token(dark, "surface")).toBe("#080d0b");
    expect(token(dark, "surface")).toBe(token(dark, "canvas"));
    expect(token(dark, "ink")).toBe("#e8f0eb");
    expect(token(dark, "fawn")).toBe("#34d399");
    expect(token(dark, "on-fawn")).toBe("#03160e");
    expect(token(dark, "shell-bg")).toBe("#070b09");
    expect(token(dark, "shell-acc")).toBe("#34d399");
    expect(token(dark, "r-sm")).toBe("2px");
    expect(token(dark, "r")).toBe("4px");
    expect(token(dark, "r-lg")).toBe("4px");
  });

  it("has the site's cool light tokens, the same under the media query and data-theme", () => {
    const light = block(':root[data-theme="light"]');
    expect(token(light, "canvas")).toBe("#f4f6f6");
    expect(token(light, "surface")).toBe(token(light, "canvas"));
    expect(token(light, "ink")).toBe("#0e1716");
    expect(token(light, "fawn")).toBe("#046e50");
    expect(token(light, "on-fawn")).toBe("#ffffff");
    const media = block(':root:not([data-theme="dark"])');
    for (const name of ["canvas", "canvas-2", "surface-2", "line", "ink", "ink-2", "ink-3", "fawn", "pass", "sap", "branch", "fail"]) {
      expect(token(media, name)).toBe(token(light, name));
    }
  });

  it("keeps the status colours (sap, pass, branch, fail) as they were, in both themes", () => {
    const dark = block(":root");
    const light = block(':root[data-theme="light"]');
    expect([token(dark, "sap"), token(dark, "pass"), token(dark, "branch"), token(dark, "fail")]).toEqual(["#e8875a", "#34d399", "#a99be0", "#f08c88"]);
    expect([token(light, "sap"), token(light, "pass"), token(light, "branch"), token(light, "fail")]).toEqual(["#9b4724", "#046e50", "#6350b3", "#a63e3a"]);
  });

  it("gives the shell its tab-strip ground and fail red as tokens", () => {
    const dark = block(":root");
    expect(token(dark, "shell-bg-2")).toBeDefined();
    expect(token(dark, "shell-bad")).toBe("#f08c88");
    expect(css).toContain("--color-shell-bg-2: var(--shell-bg-2);");
    expect(css).toContain("--color-shell-bad: var(--shell-bad);");
  });

  it("keeps no warm cream or tan grounds and inks", () => {
    for (const old of ["#f6f3ef", "#eee6dc", "#09090b", "#b9ab9b", "#948677", "#0c0c0e", "#8b7d6f"]) expect(css).not.toContain(old);
  });

  it("draws the logo in the braid's threads, not the UI accent", () => {
    expect(css).toContain(".logo .lm-in { stroke: var(--thread-in); }");
    expect(token(block(":root"), "thread-in")).toBe("#d7af87");
    expect(token(block(':root[data-theme="light"]'), "thread-in")).toBe("#86572a");
    expect(css).not.toMatch(/\.lm-\w+ \{ stroke: var\(--fawn\)/);
  });

  it("gives the main action the chip voice and keeps every other button neutral", () => {
    for (const c of ["font-mono", "text-[13px]", "text-pass", "border-pass/55", "bg-pass/4", "hover:bg-pass-soft"]) expect(main).toContain(c);
    expect(button).not.toMatch(/fawn|pass/);
    expect(button).toContain("bg-transparent");
  });

  it("draws sections as a divider, not a box, and pills as a dot and text", () => {
    expect(card).toBe("border-t border-line");
    expect(card).not.toMatch(/rounded|bg-/);
    expect(panel).toContain("border-line-strong");
    expect(pill).not.toMatch(/inset-ring|border|rounded/);
    expect(css).toContain(".pill:not(:has(.rounded-full)):not(.font-mono)::before");
    expect(chip).not.toContain("inset-ring");
  });

  it("keeps green for the action: the focus ring is branch, as the fields' focus and the selection", () => {
    expect(block("  :focus-visible")).toContain("outline: 2px solid var(--branch);");
  });
});
