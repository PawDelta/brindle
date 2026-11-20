/* The light theme's text pairs against WCAG AA, read from the real tokens: styles.css's
   light block (the one components render with under data-theme="light", which the
   prefers-color-scheme block must match) and the terminal palette ChatPane applies in light.
   Body text needs 4.5:1; the stricter 4.5 is used for every text colour here, large text
   included, since the knowledge map and the terminal set small labels in these hues too. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { chatTheme } from "./screens/Session/Chat/ChatPane";

/* Vitest runs from app/ (the app's root), where src/ holds the stylesheet. */
const css = readFileSync(resolve(process.cwd(), "src/styles.css"), "utf8");

/* The declarations of every block of one selector (the stylesheet sets some tokens in a
   second block), as name -> value. */
function block(selector: string): Record<string, string> {
  const tokens: Record<string, string> = {};
  for (let start = css.indexOf(`${selector} {`); start >= 0; start = css.indexOf(`${selector} {`, start + 1)) {
    const body = css.slice(css.indexOf("{", start) + 1, css.indexOf("}", start));
    for (const decl of body.split(";")) {
      const m = decl.match(/^\s*(--[\w-]+):\s*(.+?)\s*$/s);
      if (m) tokens[m[1]] = m[2];
    }
  }
  if (Object.keys(tokens).length === 0) throw new Error(`no ${selector} block in styles.css`);
  return tokens;
}

type Rgba = [number, number, number, number];

function parse(value: string): Rgba {
  const hex = value.match(/^#([0-9a-f]{6})$/i);
  if (hex) {
    const n = parseInt(hex[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255, 1];
  }
  const rgba = value.match(/^rgba\(([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)\)$/);
  if (rgba) return [+rgba[1], +rgba[2], +rgba[3], +rgba[4]];
  throw new Error(`not a colour the contrast check reads: ${value}`);
}

/* A colour laid over an opaque ground. */
function over(fg: Rgba, ground: Rgba): Rgba {
  const a = fg[3];
  return [0, 1, 2].map((i) => fg[i] * a + ground[i] * (1 - a)) as unknown as Rgba;
}

function luminance([r, g, b]: Rgba): number {
  const lin = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function contrast(a: Rgba, b: Rgba): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const GREEN = new Set(["pass", "fawn", "green", "brightGreen"]);
const light = block(':root[data-theme="light"]');
const media = block(':root:not([data-theme="dark"])');

/* The grounds text sits on in light: the canvas and its surfaces, and the message box's field. */
const GROUNDS = ["canvas", "canvas-2", "surface", "surface-2", "field"] as const;
/* Text colours: the ink ramp, the accents components colour text with, and the knowledge map's
   groups (text-[var(--group-N)] in the plan reader and the doc markdown). */
const TEXT = [
  "ink", "ink-2", "ink-3", "sap", "pass", "fawn", "branch", "fail",
  ...Array.from({ length: 10 }, (_, i) => `group-${i + 1}`),
] as const;

describe("light theme tokens", () => {
  it("the data-theme block and the prefers-color-scheme block agree on every colour token", () => {
    for (const [name, value] of Object.entries(light)) {
      if (name.startsWith("--line") || name === "--field") continue;
      if (name in media) expect(media[name], name).toBe(value);
    }
  });

  // The brand green is an accent (the owner accepted #078a62 at 4.36:1 on white): held to 3:1, not body-text 4.5.
  const need = (name: string) => (GREEN.has(name) ? 3 : 4.5);

  it.each(
    TEXT.flatMap((t) => GROUNDS.map((g) => [t, g] as const)),
  )("%s text on %s reaches its contrast", (text, ground) => {
    const bg = parse(light[`--${ground}`]);
    const fg = parse(light[`--${text}`]);
    expect(contrast(fg, bg)).toBeGreaterThanOrEqual(need(text));
  });

  it.each(["sap", "pass", "branch", "fail"] as const)(
    "%s-soft tint carries its text on canvas and surface-2",
    (accent) => {
      const tint = parse(light[`--${accent}-soft`]);
      const fg = parse(light[`--${accent}`]);
      for (const ground of ["canvas", "surface-2"] as const) {
        const bg = over(tint, parse(light[`--${ground}`]));
        expect(contrast(fg, bg), `${accent} on ${ground}`).toBeGreaterThanOrEqual(need(accent));
      }
    },
  );

  it.each([
    ["on-fawn", "fawn"],
    ["on-branch", "branch"],
    ["on-sap", "sap"],
  ] as const)("%s text on the %s accent fill reaches its contrast", (on, accent) => {
    expect(contrast(parse(light[`--${on}`]), parse(light[`--${accent}`]))).toBeGreaterThanOrEqual(need(accent));
  });
});

describe("light terminal palette", () => {
  const palette = chatTheme("light");
  const ground = parse(palette.background as string);
  // Every ANSI slot a program draws text in on the field. White and brightWhite are Claude Code's light-ansi
  // backgrounds (message box, user message), so they are light and checked as grounds below; text drawn in
  // them is lifted by chatContrast.
  const ansi = [
    "black", "red", "green", "yellow", "blue", "magenta", "cyan",
    "brightBlack", "brightRed", "brightGreen", "brightYellow", "brightBlue", "brightMagenta", "brightCyan",
  ] as const;

  it.each(ansi)("terminal %s text reaches its contrast on the message box's field", (slot) => {
    const fg = parse(palette[slot] as string);
    expect(contrast(fg, ground)).toBeGreaterThanOrEqual(GREEN.has(slot) ? 3 : 4.5);
  });

  it.each(["white", "brightWhite"] as const)("terminal %s stays a light ground for ink and dim text", (slot) => {
    const field = parse(palette[slot] as string);
    expect(contrast(parse(palette.foreground as string), field)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(parse(palette.brightBlack as string), field)).toBeGreaterThanOrEqual(4.5);
  });
});
