/* A text-contrast floor for the terminal's own ground only.

   xterm.js's minimumContrastRatio lifts every cell's text against that cell's ground, whatever drew the ground. In
   light that wrecks Claude Code's diffs: it draws them on its own grounds (rgb(105,219,124) and rgb(255,168,180), the
   changed words on rgb(47,157,68) and rgb(209,69,75), or the 256-colour slots nearest them) with GitHub's syntax
   colours on top, and on the word grounds no colour but black reaches 4.5:1, so a floor there turns every highlighted
   token black. Yet on the light pane's white field the same CLI's accents (its orange, its "subtle" grey, its 256-colour
   stand-ins for them) measure 2.4 to 4.4:1 and need the lift.

   So in light xterm.js's own floor is kept low (chatContrast) and this sits between the pane's bytes and xterm.js: it follows SGR state
   as the bytes go by, and while the ground is the terminal's default (no background set, not inverse) it follows any
   SGR that leaves a 256-colour or truecolour foreground below the ratio with one more SGR naming that colour lifted to
   it, as xterm.js would (darkened on a light ground, keeping its hue). When a background is set the program's colour
   is put back. The 16 named colours are the palette's, chosen to read on the field, and pass as sent. Nothing is ever
   removed or reordered: the injected SGRs only follow a sequence the program sent. */

import type { ITheme } from "@xterm/xterm";

type Rgb = readonly [number, number, number];

/* The 256-colour palette's colour for slot n (16-255): the theme's where it names one, else xterm's standard cube and
   grey ramp. */
function extended(theme: ITheme, n: number): Rgb | null {
  const given = theme.extendedAnsi?.[n - 16];
  if (given) return parseCss(given);
  if (n >= 232) {
    const v = 8 + (n - 232) * 10;
    return [v, v, v];
  }
  const levels = [0, 95, 135, 175, 215, 255];
  const i = n - 16;
  return [levels[Math.floor(i / 36)], levels[Math.floor(i / 6) % 6], levels[i % 6]];
}

function parseCss(css: string): Rgb | null {
  const hex = /^#([0-9a-f]{6})/i.exec(css);
  if (hex) {
    const v = parseInt(hex[1], 16);
    return [v >> 16, (v >> 8) & 255, v & 255];
  }
  const rgb = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i.exec(css);
  return rgb ? [Number(rgb[1]), Number(rgb[2]), Number(rgb[3])] : null;
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

/* xterm.js's ensureContrastRatio: step the text's channels towards black (on a lighter ground) or white (on a darker
   one) by a tenth at a time until it reaches the ratio, the other way if that runs out first. */
export function liftTo(ground: Rgb, fg: Rgb, ratio: number): Rgb {
  if (contrastRatio(ground, fg) >= ratio) return fg;
  const darker = (): Rgb => {
    let [r, g, b] = fg;
    while (contrastRatio(ground, [r, g, b]) < ratio && (r > 0 || g > 0 || b > 0)) {
      r -= Math.ceil(r * 0.1);
      g -= Math.ceil(g * 0.1);
      b -= Math.ceil(b * 0.1);
    }
    return [r, g, b];
  };
  const lighter = (): Rgb => {
    let [r, g, b] = fg;
    while (contrastRatio(ground, [r, g, b]) < ratio && (r < 255 || g < 255 || b < 255)) {
      r = Math.min(255, r + Math.ceil((255 - r) * 0.1));
      g = Math.min(255, g + Math.ceil((255 - g) * 0.1));
      b = Math.min(255, b + Math.ceil((255 - b) * 0.1));
    }
    return [r, g, b];
  };
  const first = luminance(fg) < luminance(ground) ? darker() : lighter();
  if (contrastRatio(ground, first) >= ratio) return first;
  const second = luminance(fg) < luminance(ground) ? lighter() : darker();
  return contrastRatio(ground, second) > contrastRatio(ground, first) ? second : first;
}

interface Pen {
  fg: string;          // the foreground as the program last set it, as SGR parameters ("39", "31", "38;5;246", "38;2;r;g;b")
  rgb: Rgb | null;     // its colour where it is a 256-colour or truecolour one, the only kind lifted
  ground: boolean;     // no background set: the cell sits on the terminal's own ground
  inverse: boolean;
  dim: boolean;
}

const FRESH: Pen = { fg: "39", rgb: null, ground: true, inverse: false, dim: false };

const enum State { Ground, Esc, EscInter, Csi, Str, StrEsc }

export class GroundContrast {
  private pen: Pen = { ...FRESH };
  private drawn = "39";   // the foreground xterm.js holds now: the program's, or the lift that followed it
  private saved: { pen: Pen; drawn: string } | null = null;
  private state = State.Ground;
  private params = "";
  private inter = "";
  private lifts = new Map<string, string>();
  private readonly encoder = new TextEncoder();

  /* theme: the terminal's colours now (its background is the ground, its extendedAnsi the 256-colour slots);
     ratio: the contrast to lift to, 1 for none. Both read on every SGR, so a theme switch applies from the next. */
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
    else if (c === 0x63) this.reset();
    return null;
  }

  private csi(final: string): string | null {
    const { params, inter } = this;
    if (final === "m" && !inter && !/^[<=>?]/.test(params)) return this.sgr(params);
    if (final === "p" && inter === "!") this.reset();
    else if ((final === "h" || final === "l") && /^\?(.*;)?104[89](;|$)/.test(params)) {
      if (final === "h") this.save();
      else return this.restore();
    } else if ((final === "s" || final === "u") && !params && !inter) {
      if (final === "s") this.save();
      else return this.restore();
    }
    return null;
  }

  private sgr(params: string): string | null {
    const pen = this.pen;
    let fgSet = false;
    const groups = params.split(";");
    for (let i = 0; i < groups.length; i++) {
      const group = groups[i];
      if (group.includes(":")) {
        const sub = group.split(":");
        const colour = sub[1] === "5" ? { n: Number(sub[2]) } : sub[1] === "2" ? { rgb: sub.slice(-3).map(Number) as unknown as Rgb } : null;
        if (sub[0] === "38" && colour) {
          fgSet = true;
          this.setFg(colour);
        } else if (sub[0] === "48") pen.ground = false;
        continue;
      }
      const code = group === "" ? 0 : Number(group);
      if (code === 38 || code === 48 || code === 58) {
        const kind = groups[i + 1];
        const colour = kind === "5" ? { n: Number(groups[i + 2]) } : kind === "2" ? { rgb: [Number(groups[i + 2]), Number(groups[i + 3]), Number(groups[i + 4])] as const } : null;
        i += kind === "5" ? 2 : kind === "2" ? 4 : 0;
        if (code === 38 && colour) {
          fgSet = true;
          this.setFg(colour);
        } else if (code === 48) pen.ground = false;
      } else if (code === 0) {
        Object.assign(pen, FRESH);
        fgSet = true;
      } else if (code === 2) pen.dim = true;
      else if (code === 22) pen.dim = false;
      else if (code === 7) pen.inverse = true;
      else if (code === 27) pen.inverse = false;
      else if ((code >= 30 && code <= 37) || (code >= 90 && code <= 97) || code === 39) {
        fgSet = true;
        pen.fg = String(code);
        pen.rgb = null;
      } else if ((code >= 40 && code <= 47) || (code >= 100 && code <= 107)) pen.ground = false;
      else if (code === 49) pen.ground = true;
    }
    if (fgSet) this.drawn = pen.fg;
    return this.redraw();
  }

  private setFg(colour: { n: number } | { rgb: Rgb }) {
    const pen = this.pen;
    if ("n" in colour) {
      pen.fg = `38;5;${colour.n}`;
      pen.rgb = colour.n >= 16 && colour.n <= 255 ? extended(this.current().theme, colour.n) : null;
    } else {
      pen.fg = `38;2;${colour.rgb.join(";")}`;
      pen.rgb = colour.rgb;
    }
  }

  /* The SGR that brings xterm.js's foreground to what this cell should be drawn in, where it differs. */
  private redraw(): string | null {
    const want = this.wanted();
    if (want === this.drawn) return null;
    this.drawn = want;
    return `\x1b[${want}m`;
  }

  private wanted(): string {
    const { pen } = this;
    const { theme, ratio } = this.current();
    if (ratio <= 1 || !pen.rgb || !pen.ground || pen.inverse) return pen.fg;
    const ground = parseCss(theme.background ?? "") ?? [0, 0, 0];
    // As xterm.js does for faint text, which it draws at half strength: half the ratio.
    const need = pen.dim ? ratio / 2 : ratio;
    const key = `${pen.rgb.join(",")}|${ground.join(",")}|${need}`;
    let lifted = this.lifts.get(key);
    if (lifted === undefined) {
      const rgb = liftTo(ground, pen.rgb, need);
      lifted = rgb === pen.rgb ? "" : `38;2;${rgb.join(";")}`;
      this.lifts.set(key, lifted);
    }
    return lifted || pen.fg;
  }

  private save() {
    this.saved = { pen: { ...this.pen }, drawn: this.drawn };
  }

  private restore(): string | null {
    if (!this.saved) {
      this.reset();
      return null;
    }
    this.pen = { ...this.saved.pen };
    this.drawn = this.saved.drawn;
    return this.redraw();
  }

  private reset() {
    this.pen = { ...FRESH };
    this.drawn = "39";
  }
}
