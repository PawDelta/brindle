/* Light panes' text contrast, set in the byte stream: every cell's text reaches the ratio (WCAG AA, 4.5:1) against the
   ground it is drawn on, by its lightness alone.

   xterm.js's own floor (minimumContrastRatio) steps a colour's channels towards black or white, and on mid-tone grounds
   that flattens everything: Claude Code's light diff draws its changed words on rgb(47,157,68) and rgb(209,69,75),
   where xterm.js turned every syntax colour black or white and the diff lost its keywords, strings and numbers. Here
   the colour keeps its hue and saturation (HSL) and only its lightness moves, the least that reaches the ratio, so each
   token keeps its own colour wherever the ground leaves room, and add and remove keep their grounds as sent.

   Faint text (SGR 2) is xterm.js's text at half strength, which on white can't reach 4.5:1 whatever its colour. While
   this lifts, faint is taken off in xterm.js (SGR 22, bold put back) and the cell is drawn in the colour faint would
   give (the text halfway to its ground), lifted to the ratio like any other.

   It sits between the pane's bytes and xterm.js and follows the program's SGR state as they go by (colours, bold,
   faint, inverse, and their save and restore per screen buffer as xterm.js keeps them). After each SGR the program
   sends it adds one SGR where xterm.js's foreground or faint should differ from the program's; nothing the program
   sent is removed or reordered. Inverse cells (a cursor, a selection) are drawn as sent. Ratio 1: nothing is added. */

import type { ITheme } from "@xterm/xterm";

export type Rgb = readonly [number, number, number];

const NAMED = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
  "brightBlack", "brightRed", "brightGreen", "brightYellow", "brightBlue", "brightMagenta", "brightCyan", "brightWhite"] as const;
/* xterm.js's own 16 colours, where a theme names none (its defaults). */
const XTERM_16 = ["#2e3436", "#cc0000", "#4e9a06", "#c4a000", "#3465a4", "#75507b", "#06989a", "#d3d7cf",
  "#555753", "#ef2929", "#8ae234", "#fce94f", "#729fcf", "#ad7fa8", "#34e2e2", "#eeeeec"];

export function parseCss(css: string | undefined): Rgb | null {
  if (!css) return null;
  const hex = /^#([0-9a-f]{6})/i.exec(css);
  if (hex) {
    const v = parseInt(hex[1], 16);
    return [v >> 16, (v >> 8) & 255, v & 255];
  }
  const rgb = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i.exec(css);
  return rgb ? [Number(rgb[1]), Number(rgb[2]), Number(rgb[3])] : null;
}

/* Palette slot n (0-255) as the theme draws it: its named colours, its extendedAnsi, else xterm's cube and greys. */
function slot(theme: ITheme, n: number): Rgb {
  if (n < 16) return parseCss(theme[NAMED[n]]) ?? parseCss(XTERM_16[n])!;
  const given = parseCss(theme.extendedAnsi?.[n - 16]);
  if (given) return given;
  if (n >= 232) {
    const v = 8 + (n - 232) * 10;
    return [v, v, v];
  }
  const levels = [0, 95, 135, 175, 215, 255];
  const i = n - 16;
  return [levels[Math.floor(i / 36)], levels[Math.floor(i / 6) % 6], levels[i % 6]];
}

function luminance([r, g, b]: Rgb): number {
  const lin = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

export function contrastRatio(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

export function toHsl([r, g, b]: Rgb): [number, number, number] {
  const [rr, gg, bb] = [r / 255, g / 255, b / 255];
  const max = Math.max(rr, gg, bb), min = Math.min(rr, gg, bb);
  const l = (max + min) / 2;
  if (max === min) return [0, 0, l];
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  const h = max === rr ? (gg - bb) / d + (gg < bb ? 6 : 0) : max === gg ? (bb - rr) / d + 2 : (rr - gg) / d + 4;
  return [h * 60, s, l];
}

function fromHsl(h: number, s: number, l: number): Rgb {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = l - c / 2;
  const [r, g, b] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x] : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
  return [Math.round((r + m) * 255), Math.round((g + m) * 255), Math.round((b + m) * 255)];
}

/* fg with its HSL lightness moved the least that reaches ratio on ground, towards black where fg is the darker of
   the two (else towards white); the other way where that way can't get there; as far as either can where neither can. */
export function liftLightness(ground: Rgb, fg: Rgb, ratio: number): Rgb {
  if (contrastRatio(ground, fg) >= ratio) return fg;
  const [h, s, l] = toHsl(fg);
  const towards = (end: 0 | 1): Rgb => {
    // Binary search between fg's lightness and the end for the nearest lightness that reaches the ratio.
    let near = l, far: number = end;
    if (contrastRatio(ground, fromHsl(h, s, far)) < ratio) return fromHsl(h, s, far);
    for (let i = 0; i < 24; i++) {
      const mid = (near + far) / 2;
      if (contrastRatio(ground, fromHsl(h, s, mid)) >= ratio) far = mid;
      else near = mid;
    }
    return fromHsl(h, s, far);
  };
  const first = luminance(fg) <= luminance(ground) ? 0 : 1;
  const one = towards(first);
  if (contrastRatio(ground, one) >= ratio) return one;
  const other = towards(first === 0 ? 1 : 0);
  return contrastRatio(ground, other) > contrastRatio(ground, one) ? other : one;
}

/* A colour as the program set it: the terminal's default, a palette slot, or truecolour; and the SGR that set it. */
type Colour = { kind: "default" } | { kind: "slot"; n: number } | { kind: "rgb"; rgb: Rgb };

interface Pen {
  fg: Colour;
  fgSgr: string;       // the SGR parameters that set fg ("39", "31", "38;5;246", "38;2;r;g;b")
  bg: Colour;
  bgSgr: string;       // and bg ("49", "41", "48;5;167", "48;2;r;g;b")
  bold: boolean;
  dim: boolean;
  inverse: boolean;
}

/* What xterm.js holds of what this changes: its foreground and background SGRs and whether it draws faint. */
interface Held { fgSgr: string; bgSgr: string; dim: boolean }
interface Saved { pen: Pen; held: Held }

const freshPen = (): Pen => ({ fg: { kind: "default" }, fgSgr: "39", bg: { kind: "default" }, bgSgr: "49", bold: false, dim: false, inverse: false });
const freshHeld = (): Held => ({ fgSgr: "39", bgSgr: "49", dim: false });

/* A ground the program set that is mid-tone (luminance between MID_GROUND[0] and [1]) leaves text no room to keep its
   hue at 4.5:1: only near-black or near-white reaches it. Claude Code's light diff draws its changed words on two such
   grounds (rgb(209,69,75) at 0.18, rgb(47,157,68) at 0.25). While lifting, such a ground is drawn lighter in its own hue,
   at MID_GROUND[1], where text keeps its hue at 4.5:1 and the ground stays darker and more saturated than its line's
   (Claude's line grounds sit at 0.5 to 0.6). Darker grounds (black, a status bar's) are a program's choice and stay. */
const MID_GROUND = [0.1, 0.45] as const;

/* ground in its own hue, its HSL lightness raised until its luminance reaches `target`. A red keeps its saturation at
   0.85 at least: a light red with less reads pink (a removed word must read as removed); greens keep their own. */
export function lightenTo(ground: Rgb, target: number): Rgb {
  const [h, s0, l] = toHsl(ground);
  const s = h >= 345 || h < 15 ? Math.max(s0, 0.85) : s0;
  let low = l, high = 1;
  for (let i = 0; i < 24; i++) {
    const mid = (low + high) / 2;
    if (luminance(fromHsl(h, s, mid)) >= target) high = mid;
    else low = mid;
  }
  return fromHsl(h, s, high);
}

export const groundLuminance = (rgb: Rgb) => luminance(rgb);
const fresh = (): Saved => ({ pen: freshPen(), held: freshHeld() });

const enum State { Ground, Esc, EscInter, Csi, Str, StrEsc }

export class TextContrast {
  private pen = freshPen();
  private held = freshHeld();
  // xterm.js's saved cursor state, one per screen buffer (DECSC / CSI s / mode 1048, and mode 1049 in the normal one).
  private saved: Record<"normal" | "alt", Saved> = { normal: fresh(), alt: fresh() };
  private buffer: "normal" | "alt" = "normal";
  private state = State.Ground;
  private params = "";
  private inter = "";
  private lifts = new Map<string, string>();
  private readonly encoder = new TextEncoder();

  /* theme: the terminal's colours now; ratio: what text reaches against its ground, 1 for nothing added. Both read on
     every SGR, so a theme switch applies from the next one. */
  constructor(private readonly current: () => { theme: ITheme; ratio: number }) {}

  feed(bytes: Uint8Array): Uint8Array {
    let out: Uint8Array[] | null = null;
    let from = 0;
    for (let i = 0; i < bytes.length; i++) {
      const added = this.step(bytes[i]);
      if (added) {
        (out ??= []).push(bytes.subarray(from, i + 1), this.encoder.encode(added));
        from = i + 1;
      }
    }
    if (!out) return bytes;
    out.push(bytes.subarray(from));
    const joined = new Uint8Array(out.reduce((n, part) => n + part.length, 0));
    let at = 0;
    for (const part of out) {
      joined.set(part, at);
      at += part.length;
    }
    return joined;
  }

  /* One byte through the escape-sequence parser; what to send after it, if anything. */
  private step(c: number): string | null {
    switch (this.state) {
      case State.Ground:
        if (c === 0x1b) this.state = State.Esc;
        return null;
      case State.Esc:
        return this.escape(c);
      case State.EscInter:
        if (c >= 0x30 && c <= 0x7e) this.state = State.Ground;
        else if (c === 0x1b) this.state = State.Esc;
        return null;
      case State.Csi:
        if (c >= 0x30 && c <= 0x3f) this.params += String.fromCharCode(c);
        else if (c >= 0x20 && c <= 0x2f) this.inter += String.fromCharCode(c);
        else if (c >= 0x40 && c <= 0x7e) {
          this.state = State.Ground;
          return this.csi(String.fromCharCode(c));
        } else if (c === 0x1b) this.state = State.Esc;
        else if (c === 0x18 || c === 0x1a) this.state = State.Ground;
        return null;
      case State.Str:
        if (c === 0x07) this.state = State.Ground;
        else if (c === 0x1b) this.state = State.StrEsc;
        return null;
      case State.StrEsc:
        if (c === 0x5c) {
          this.state = State.Ground;
          return null;
        }
        return this.escape(c);
    }
  }

  private escape(c: number): string | null {
    this.state = State.Ground;
    if (c === 0x5b) {
      this.state = State.Csi;
      this.params = "";
      this.inter = "";
    } else if (c === 0x5d || c === 0x50 || c === 0x58 || c === 0x5e || c === 0x5f) this.state = State.Str;
    else if (c >= 0x20 && c <= 0x2f) this.state = State.EscInter;
    else if (c === 0x1b) this.state = State.Esc;
    else if (c === 0x37) this.save();
    else if (c === 0x38) return this.restore();
    else if (c === 0x63) this.fullReset();
    return null;
  }

  private csi(final: string): string | null {
    const { params, inter } = this;
    if (final === "m" && !inter && !/^[<=>?]/.test(params)) return this.sgr(params);
    if (final === "p" && inter === "!" && /^[\d;:]*$/.test(params)) {
      // DECSTR, as xterm.js does it (any numeric parameters, CSI 0 ! p included; a private prefix is another
      // sequence): attributes back to default, and the active buffer's saved ones with them.
      this.pen = freshPen();
      this.held = freshHeld();
      this.saved[this.buffer] = fresh();
      return null;
    }
    if (final === "s" && !params && !inter) this.save();
    else if (final === "u" && !params && !inter) return this.restore();
    else if ((final === "h" || final === "l") && !inter && params.startsWith("?")) {
      let after: string | null = null;
      for (const mode of params.slice(1).split(";")) {
        if (final === "h") {
          if (mode === "1048" || mode === "1049") this.save();
          if (mode === "1049" || mode === "1047" || mode === "47") this.buffer = "alt";
        } else {
          if (mode === "1049" || mode === "1047" || mode === "47") this.buffer = "normal";
          if (mode === "1048" || mode === "1049") after = this.restore() ?? after;
        }
      }
      return after;
    }
    return null;
  }

  private sgr(params: string): string | null {
    const pen = this.pen;
    let fgSet = false;
    let bgSet = false;
    let dimTouched = false;
    const groups = params.split(";");
    for (let i = 0; i < groups.length; i++) {
      const group = groups[i];
      if (group.includes(":")) {
        const sub = group.split(":");
        const colour: Colour | null = sub[1] === "5" ? { kind: "slot", n: Number(sub[2]) }
          : sub[1] === "2" ? { kind: "rgb", rgb: sub.slice(-3).map(Number) as unknown as Rgb } : null;
        if (sub[0] === "38" && colour) {
          fgSet = true;
          pen.fg = colour;
          pen.fgSgr = colour.kind === "slot" ? `38;5;${colour.n}` : `38;2;${(colour as { rgb: Rgb }).rgb.join(";")}`;
        } else if (sub[0] === "48" && colour) {
          bgSet = true;
          pen.bg = colour;
          pen.bgSgr = colour.kind === "slot" ? `48;5;${colour.n}` : `48;2;${(colour as { rgb: Rgb }).rgb.join(";")}`;
        }
        continue;
      }
      const code = group === "" ? 0 : Number(group);
      if (code === 38 || code === 48 || code === 58) {
        const kind = groups[i + 1];
        const colour: Colour | null = kind === "5" ? { kind: "slot", n: Number(groups[i + 2]) }
          : kind === "2" ? { kind: "rgb", rgb: [Number(groups[i + 2]), Number(groups[i + 3]), Number(groups[i + 4])] } : null;
        i += kind === "5" ? 2 : kind === "2" ? 4 : 0;
        if (code === 38 && colour) {
          fgSet = true;
          pen.fg = colour;
          pen.fgSgr = colour.kind === "slot" ? `38;5;${colour.n}` : `38;2;${(colour as { rgb: Rgb }).rgb.join(";")}`;
        } else if (code === 48 && colour) {
          bgSet = true;
          pen.bg = colour;
          pen.bgSgr = colour.kind === "slot" ? `48;5;${colour.n}` : `48;2;${(colour as { rgb: Rgb }).rgb.join(";")}`;
        }
      } else if (code === 0) {
        Object.assign(pen, freshPen());
        fgSet = bgSet = dimTouched = true;
      } else if (code === 1) pen.bold = true;
      else if (code === 2) {
        pen.dim = true;
        dimTouched = true;
      } else if (code === 22) {
        pen.bold = pen.dim = false;
        dimTouched = true;
      } else if (code === 7) pen.inverse = true;
      else if (code === 27) pen.inverse = false;
      else if ((code >= 30 && code <= 37) || (code >= 90 && code <= 97)) {
        fgSet = true;
        pen.fg = { kind: "slot", n: code >= 90 ? code - 82 : code - 30 };
        pen.fgSgr = String(code);
      } else if (code === 39) {
        fgSet = true;
        pen.fg = { kind: "default" };
        pen.fgSgr = "39";
      } else if ((code >= 40 && code <= 47) || (code >= 100 && code <= 107) || code === 49) {
        bgSet = true;
        pen.bg = code === 49 ? { kind: "default" } : { kind: "slot", n: code >= 100 ? code - 92 : code - 40 };
        pen.bgSgr = String(code);
      }
    }
    // What xterm.js holds now: the program's colours and faint where this SGR set them, what it held before otherwise.
    if (fgSet) this.held.fgSgr = pen.fgSgr;
    if (bgSet) this.held.bgSgr = pen.bgSgr;
    if (dimTouched) this.held.dim = pen.dim;
    return this.redraw();
  }

  /* The SGR that brings xterm.js's foreground and faint to what this cell should be drawn in, where they differ. */
  private redraw(): string | null {
    const { pen, held } = this;
    const { theme, ratio } = this.current();
    const lifting = ratio > 1 && !pen.inverse;
    const ground = lifting ? this.ground(theme) : null;
    const wantDim = lifting ? false : pen.dim;
    const wantBg = ground?.lightened ? `48;2;${ground.rgb.join(";")}` : pen.bgSgr;
    const wantFg = ground ? this.liftedFg(theme, ground.rgb, ratio) : pen.fgSgr;
    const parts: string[] = [];
    if (held.dim && !wantDim) parts.push(pen.bold ? "22;1" : "22");   // 22 ends bold too: put it back
    if (!held.dim && wantDim) parts.push("2");
    if (held.bgSgr !== wantBg) parts.push(wantBg);
    if (held.fgSgr !== wantFg) parts.push(wantFg);
    held.dim = wantDim;
    held.bgSgr = wantBg;
    held.fgSgr = wantFg;
    return parts.length ? `\x1b[${parts.join(";")}m` : null;
  }

  /* The cell's ground as it should be drawn: the program's, or a mid-tone one it set lightened (MID_GROUND). */
  private ground(theme: ITheme): { rgb: Rgb; lightened: boolean } {
    const rgb = this.colour(theme, this.pen.bg, false);
    if (this.pen.bg.kind === "default") return { rgb, lightened: false };
    const lum = luminance(rgb);
    if (lum < MID_GROUND[0] || lum >= MID_GROUND[1]) return { rgb, lightened: false };
    const key = `ground|${rgb.join(",")}`;
    let lighter = this.lifts.get(key);
    if (lighter === undefined) this.lifts.set(key, (lighter = lightenTo(rgb, MID_GROUND[1]).join(",")));
    return { rgb: lighter.split(",").map(Number) as unknown as Rgb, lightened: true };
  }

  private liftedFg(theme: ITheme, ground: Rgb, ratio: number): string {
    const { pen } = this;
    let text = this.colour(theme, pen.fg, true);
    // Faint as xterm.js draws it: the text halfway to its ground.
    if (pen.dim) text = text.map((c, i) => Math.round((c + ground[i]) / 2)) as unknown as Rgb;
    const key = `${text.join(",")}|${ground.join(",")}|${ratio}`;
    let lifted = this.lifts.get(key);
    if (lifted === undefined) {
      const rgb = liftLightness(ground, text, ratio);
      lifted = rgb === text ? "" : `38;2;${rgb.join(";")}`;
      this.lifts.set(key, lifted);
    }
    // Faint is taken off in xterm.js, so a faint cell always names its colour.
    return lifted || (pen.dim ? `38;2;${text.join(";")}` : pen.fgSgr);
  }

  private colour(theme: ITheme, colour: Colour, fg: boolean): Rgb {
    if (colour.kind === "rgb") return colour.rgb;
    if (colour.kind === "default") return parseCss(fg ? theme.foreground : theme.background) ?? (fg ? [255, 255, 255] : [0, 0, 0]);
    // Bold text in the first eight colours is drawn in their bright twins (xterm.js's drawBoldTextInBrightColors).
    return slot(theme, fg && this.pen.bold && colour.n < 8 ? colour.n + 8 : colour.n);
  }

  private save() {
    this.saved[this.buffer] = { pen: { ...this.pen }, held: { ...this.held } };
  }

  /* xterm.js's restore: the buffer's saved attributes, the defaults where nothing was saved. */
  private restore(): string | null {
    const { pen, held } = this.saved[this.buffer];
    this.pen = { ...pen };
    this.held = { ...held };
    return this.redraw();
  }

  private fullReset() {
    this.pen = freshPen();
    this.held = freshHeld();
    this.saved = { normal: fresh(), alt: fresh() };
    this.buffer = "normal";
  }
}
