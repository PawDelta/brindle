/* Where a visual's page renders: one interface, and the component behind it.

   The page runs in a separate child webview on the brindle-visual:// scheme (owner amendment 8:
   design/visualizer-build-plan-amendments.md), so a hung page can't freeze the app's own webview.
   The shell's side is app/src-tauri/src/visual_scheme.rs. Browser development uses a placeholder.

   The host, not the page, says which visual a message came from (the child webview's label), and
   the reader drops one from any other: a page can't speak for another visual or an older revision. */

import type { ComponentType } from "react";
import { isTauri } from "@tauri-apps/api/core";
import { NativeFrame } from "./NativeFrame";

/* What the frame shows: a session's visual at a revision, or a kept copy (Knowledge › Kept visuals). */
export type VisualSource = { kind: "session"; id: string; rev: number } | { kind: "kept"; id: string };

/* The scheme's URL for a source (WebView2 maps it to http://brindle-visual.localhost/…). */
export function visualSrc(source: VisualSource): string {
  return source.kind === "kept" ? `brindle-visual://kept/${source.id}` : `brindle-visual://${source.id}/r${source.rev}`;
}

export const sameSource = (a: VisualSource, b: VisualSource): boolean =>
  a.kind === b.kind && a.id === b.id && (a.kind === "kept" || (b.kind === "session" && a.rev === b.rev));

/* What a page may post to the host (the wave-1 contract's frame → host). A snapshot is worker 5's (export). */
export type FrameMessage = { type: "choice"; option: string } | { type: "brindle.esc" } | { type: "brindle.snapshot"; html: string };

export function frameMessage(data: unknown): FrameMessage | null {
  if (!data || typeof data !== "object") return null;
  const m = data as { type?: unknown; option?: unknown; html?: unknown };
  if (m.type === "brindle.snapshot" && typeof m.html === "string") return { type: "brindle.snapshot", html: m.html };
  if (m.type === "brindle.esc") return { type: "brindle.esc" };
  if (m.type === "choice" && typeof m.option === "string") return { type: "choice", option: m.option };
  return null;
}

/* Export fidelity. A snapshot is the page's outerHTML, and the daemon's sanitiser (visual_export.py)
   keeps only literal presentation values: no var(), and only colour-scheme @media. So before it
   goes, the host renders the snapshot inert (no script, nothing fetched) in a hidden frame, reads
   each element's computed presentation values in the light and in the dark scheme, and writes them
   as literal classes behind a prefers-color-scheme switch. The page's own declarations of those
   properties are removed, so what's left (layout) can't contradict them. */

const SVG = "http://www.w3.org/2000/svg";
const SIDES = ["top", "right", "bottom", "left"];
const CORNERS = ["top-left", "top-right", "bottom-right", "bottom-left"];
/* Inherited text values: written for every element, so no user-agent default can differ. */
const TEXT = ["color", "font-family", "font-size", "font-weight", "font-style", "font-variant-numeric", "line-height",
  "letter-spacing", "text-transform", "text-align", "white-space"];
/* Box values, written only when they aren't the initial value in either scheme. */
const BOX = ["background-color", "background-image", ...SIDES.flatMap((side) => ["style", "width", "color"].map((part) => `border-${side}-${part}`)),
  ...CORNERS.map((corner) => `border-${corner}-radius`), "box-shadow", "opacity", "text-decoration-line"];
/* SVG references (a gradient, an arrowhead) go back as attributes, and only to the page's own #id. */
const REFERENCES = ["fill", "stroke", "marker-start", "marker-mid", "marker-end"];
const PAINT = ["fill", "fill-opacity", "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap", "stroke-linejoin",
  "stroke-opacity", "text-anchor", "marker-start", "marker-mid", "marker-end"];
const STOP = ["stop-color", "stop-opacity"];
/* Layout values the browser answers as declared (variables resolved), whatever the window's size. */
const LAYOUT = ["display", "position", "box-sizing", "overflow-x", "overflow-y", "flex-direction", "flex-wrap", "flex-grow",
  "flex-shrink", "flex-basis", "order", "align-items", "align-content", "align-self", "justify-content", "justify-items",
  "justify-self", "row-gap", "column-gap", "grid-auto-flow", "grid-column-start", "grid-column-end", "grid-row-start",
  "grid-row-end", "min-width", "max-width", "min-height", "max-height", "z-index", "vertical-align", "table-layout",
  "border-collapse", "border-spacing", "list-style-type", "text-overflow", "word-break", "overflow-wrap", "visibility", "aspect-ratio"];
/* Layout values the browser answers in pixels for this window. The page's own declarations of them
   stay (a percentage or an auto margin keeps working at any width), and a literal is written only
   where the export wouldn't otherwise reproduce them: see inlineStyles. */
const SIZED = [...SIDES.flatMap((side) => [`margin-${side}`, `padding-${side}`]), ...SIDES, "grid-template-columns"];
/* What visual_export.py keeps of the page's own layout CSS: a subset of its property allowlist and
   its value and selector rules, so what counts as kept here is kept there too. */
const KEPT = new Set([...SIZED, "display", "position", "width", "height", "min-width", "max-width", "min-height", "max-height",
  "box-sizing", "flex-direction", "flex-wrap", "flex-grow", "flex-shrink", "flex-basis", "order", "row-gap", "column-gap",
  "grid-template-rows", "grid-auto-flow", "grid-auto-columns", "grid-auto-rows", "grid-column-start", "grid-column-end",
  "grid-row-start", "grid-row-end", "align-items", "align-content", "align-self", "justify-content", "justify-items", "justify-self",
  "overflow-x", "overflow-y", "aspect-ratio", "table-layout", "border-collapse", "border-spacing", "vertical-align", "white-space",
  "font-size", "line-height", "letter-spacing"]);
const KEPT_FUNCTIONS = new Set(["repeat", "minmax", "rgb", "rgba", "hsl", "hsla", "hwb", "lab", "lch", "oklab", "oklch", "color",
  "color-mix", "calc", "min", "max", "clamp", "fit-content", "linear-gradient", "radial-gradient", "conic-gradient",
  "repeating-linear-gradient", "repeating-radial-gradient"]);
const FLAT = /^[A-Za-z0-9_#., >+~*-]+$/;
const PSEUDO_ELEMENT = /::?(?:before|after)(?=$|[\s,>+~])/g;
const SCHEME_MEDIA = /^\(\s*prefers-color-scheme\s*:\s*(?:light|dark)\s*\)$/i;
const INITIAL: Record<string, string[]> = {
  "background-color": ["rgba(0, 0, 0, 0)", "transparent"], "background-image": ["none"], "box-shadow": ["none"],
  opacity: ["1"], "text-decoration-line": ["none"], ...Object.fromEntries(CORNERS.map((corner) => [`border-${corner}-radius`, ["0px"]])),
  content: ["none"], "marker-start": ["none"], "marker-mid": ["none"], "marker-end": ["none"],
  position: ["static"], "box-sizing": ["content-box"], "flex-direction": ["row"], "flex-wrap": ["nowrap"], "flex-grow": ["0"],
  "flex-shrink": ["1"], "flex-basis": ["auto"], order: ["0"], "align-items": ["normal"], "align-content": ["normal"],
  "align-self": ["auto"], "justify-content": ["normal"], "justify-items": ["normal", "legacy"], "justify-self": ["auto"],
  "row-gap": ["normal"], "column-gap": ["normal"], "grid-auto-flow": ["row"], "grid-column-start": ["auto"],
  "grid-column-end": ["auto"], "grid-row-start": ["auto"], "grid-row-end": ["auto"], "min-width": ["auto", "0px"],
  "max-width": ["none"], "min-height": ["auto", "0px"], "max-height": ["none"], "z-index": ["auto"], "table-layout": ["auto"],
  "text-overflow": ["clip"], "word-break": ["normal"], "overflow-wrap": ["normal"], visibility: ["visible"],
  "aspect-ratio": ["auto"],
};
/* The user agent styles these itself, so their box values are always written. */
const UA_BOXED = new Set(["a", "u", "s", "ins", "del", "hr", "mark", "abbr"]);
/* The page's own declarations that the captured values replace (shorthands included). Sized values
   aren't: where they depend on the window, the page's declaration is what's kept. */
const REPLACED = new Set([...TEXT, ...BOX, ...PAINT, ...STOP, ...LAYOUT, "content", "background", "border", "border-color", "border-style",
  "border-width", "border-radius", ...SIDES.map((side) => `border-${side}`), "font", "text-decoration", "text-decoration-color",
  "text-decoration-style", "marker", "overflow", "flex", "flex-flow", "gap", "place-items", "place-content", "place-self",
  "grid-column", "grid-row", "grid-area"]);
const NOT_STYLED = new Set(["head", "style", "script", "template", "noscript"]);
const FETCHES = /url\s*\(|image-set|image\(|\\|</i;
const QUOTED = /^"[^"\\]*"$/;

export type ColorScheme = "light" | "dark";
/* What getComputedStyle answers, for one element (or one of its pseudo-elements). */
export type StyleReader = (element: Element, pseudo?: string) => { getPropertyValue(name: string): string };
type Captured = Record<string, string>;

/* Make the document's own prefers-color-scheme conditions answer `scheme`. Returns the undo. */
export function forceScheme(doc: Document, scheme: ColorScheme): () => void {
  const undo: (() => void)[] = [];
  const rewrite = (media: MediaList | null | undefined) => {
    const text = media?.mediaText ?? "";
    if (!media || !/prefers-color-scheme/i.test(text)) return;
    media.mediaText = text.replace(/\(\s*prefers-color-scheme\s*:\s*(light|dark)\s*\)/gi,
      (_, wanted: string) => wanted.toLowerCase() === scheme ? "(min-width: 0px)" : "(max-width: 0px)");
    undo.push(() => { media.mediaText = text; });
  };
  const visit = (rules: CSSRuleList) => {
    for (const rule of Array.from(rules)) {
      rewrite((rule as CSSMediaRule).media);
      if ("cssRules" in rule) visit((rule as CSSGroupingRule).cssRules);
    }
  };
  for (const sheet of Array.from(doc.styleSheets)) {
    rewrite(sheet.media);
    try { visit(sheet.cssRules); } catch { /* a sheet the frame can't read has nothing to rewrite */ }
  }
  const root = (doc.documentElement as HTMLElement).style;
  const prior = root?.getPropertyValue("color-scheme") ?? "";
  root?.setProperty("color-scheme", scheme);
  undo.push(() => prior ? root.setProperty("color-scheme", prior) : root.removeProperty("color-scheme"));
  return () => { for (const step of undo.reverse()) step(); };
}

const values = (style: ReturnType<StyleReader>, list: string[]): Captured =>
  Object.fromEntries(list.map((name) => [name, style.getPropertyValue(name).trim()]));

/* Whether an element takes part in the page's layout (an SVG shape inside a drawing doesn't). */
const laidOut = (element: Element) => element.namespaceURI !== SVG || element.localName === "svg";

/* An element and its ::before and ::after. A pseudo-element that isn't there is `content: none`,
   so a scheme that has one and a scheme that doesn't can be told apart. */
function capture(element: Element, read: StyleReader): Captured[] {
  const paint = element.namespaceURI !== SVG ? [] : element.localName === "stop" ? STOP : PAINT;
  const parts = [values(read(element), [...TEXT, ...BOX, ...paint, ...(laidOut(element) ? [...LAYOUT, ...SIZED] : [])])];
  for (const pseudo of ["::before", "::after"]) {
    const style = read(element, pseudo);
    const content = style.getPropertyValue("content").trim();
    parts.push(QUOTED.test(content) ? { content, ...values(style, [...TEXT, "background-color"]) } : { content: "none" });
  }
  return parts;
}

/* What `value` names if it is a reference to an element of `doc` itself (url(#id)), else null. */
export function localReference(value: string | undefined, doc: Document): string | null {
  const target = /^url\(\s*(["']?)([^"'()\s]*)\1\s*\)$/.exec(value ?? "")?.[2];
  if (!target) return null;
  const hash = target.indexOf("#");
  const before = target.slice(0, hash);
  const id = target.slice(hash + 1);
  if (hash < 0 || (before && before !== doc.URL && before !== doc.baseURI) || !/^[A-Za-z_][\w-]*$/.test(id)) return null;
  return id;
}

/* The declarations to write for one element part: every light value, then what differs in dark
   (including a value only one scheme has). */
function declarations(tag: string, light: Captured, dark: Captured): [string, string] {
  const omitted = new Set<string>();
  for (const side of SIDES) {
    const style = `border-${side}-style`;
    if (!UA_BOXED.has(tag) && [light[style], dark[style]].every((value) => !value || value === "none" || value === "hidden")) {
      for (const part of ["style", "width", "color"]) omitted.add(`border-${side}-${part}`);
    }
  }
  const lit: string[] = [];
  const dim: string[] = [];
  for (const name of new Set([...Object.keys(light), ...Object.keys(dark)])) {
    if (omitted.has(name)) continue;
    const usable = (value?: string) => !!value && !FETCHES.test(value)
      && (name !== "background-image" || /gradient\(/.test(value)) && (name !== "content" || value === "none" || QUOTED.test(value));
    const [l, d] = [light[name], dark[name]].map((value) => usable(value) ? value : "");
    const initial = UA_BOXED.has(tag) && name !== "content" ? undefined : INITIAL[name];
    if (initial && [l, d].every((value) => !value || initial.includes(value))) continue;
    if (l) lit.push(`${name}:${l}`);
    if (d && d !== l) dim.push(`${name}:${d}`);
  }
  return [lit.join(";"), dim.join(";")];
}

/* Remove the declarations the captured values replace from a declaration block. */
function strip(style: CSSStyleDeclaration | undefined) {
  if (!style) return;
  for (let i = style.length - 1; i >= 0; i--) if (REPLACED.has(style.item(i))) style.removeProperty(style.item(i));
  for (const name of REPLACED) style.removeProperty(name);
}

function stripRules(rules: CSSRuleList) {
  for (const rule of Array.from(rules)) {
    strip((rule as CSSStyleRule).style);
    if ("cssRules" in rule) stripRules((rule as CSSGroupingRule).cssRules);
  }
}

/* Whether visual_export.py keeps a value (its value rules: no url(), escapes or unlisted functions). */
function keptValue(value: string): boolean {
  if (/url\s*\(/i.test(value)) return false;
  const plain = value.replace(/"[^"'\\<>@;{}\r\n]*"|'[^"'\\<>@;{}\r\n]*'/g, "_");
  return /^[\w\s#.,%+\-/*()]+$/.test(plain) && Array.from(plain.matchAll(/([\w-]+)\s*\(/g)).every((call) => KEPT_FUNCTIONS.has(call[1].toLowerCase()));
}

function keptDeclarations(style: CSSStyleDeclaration): string {
  const kept: string[] = [];
  for (let i = 0; i < style.length; i++) {
    const name = style.item(i);
    const value = style.getPropertyValue(name).trim();
    if (KEPT.has(name) && value && keptValue(value)) kept.push(`${name}:${value}${style.getPropertyPriority(name) ? " !important" : ""}`);
  }
  return kept.join(";");
}

/* The layout part of the page's own CSS that the export keeps: flat rules, and flat rules behind a
   colour-scheme @media. */
function keptCss(rules: CSSRuleList, nested = false): string {
  let out = "";
  for (const rule of Array.from(rules)) {
    const { selectorText, style } = rule as CSSStyleRule;
    if (typeof selectorText === "string" && style) {
      const body = keptDeclarations(style);
      if (body && FLAT.test(selectorText.replace(PSEUDO_ELEMENT, ""))) out += `${selectorText}{${body}}`;
      continue;
    }
    const media = (rule as CSSMediaRule).media?.mediaText ?? "";
    const inner = !nested && SCHEME_MEDIA.test(media) ? keptCss((rule as CSSMediaRule).cssRules, true) : "";
    if (inner) out += `@media ${media}{${inner}}`;
  }
  return out;
}

/* A paint or marker value as an SVG attribute the sanitiser keeps (no parentheses): none or hex. */
function attributeValue(value: string | undefined): string | null {
  if (value === "none") return "none";
  const rgb = /^rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)$/.exec(value ?? "");
  if (!rgb) return null;
  const hex = (n: number) => Math.round(n).toString(16).padStart(2, "0");
  return `#${hex(+rgb[1])}${hex(+rgb[2])}${hex(+rgb[3])}${rgb[4] === undefined ? "" : hex(+rgb[4] * 255)}`;
}

/* An element's references that differ by scheme: the attributes its light and its dark copy carry. */
type Split = Record<ColorScheme, [string, string | null][]>;

/* SVG paint and markers that point at the page's own #id become attributes (CSS can't carry them
   through the sanitiser); any other url() is dropped. Where light and dark differ, the returned
   split says what each scheme's copy of the element carries (see splitBySchemes). */
function settleReferences(element: Element, light: Captured, dark: Captured): Split | null {
  const doc = element.ownerDocument;
  const references = REFERENCES.map((name) => [name, localReference(light[name], doc), localReference(dark[name], doc)] as const);
  const differs = element.localName !== "svg" && references.some(([, lit, dim]) => lit !== dim);
  const split: Split = { light: [], dark: [] };
  for (const [name, lit, dim] of references) {
    if (!lit && !dim) continue;
    if (lit === dim || !differs) {
      if (lit) element.setAttribute(name, `url(#${lit})`);
    } else {
      split.light.push([name, lit ? `url(#${lit})` : attributeValue(light[name])]);
      split.dark.push([name, dim ? `url(#${dim})` : attributeValue(dark[name])]);
    }
    delete light[name];
    delete dark[name];
  }
  return differs ? split : null;
}

/* Each outermost split element becomes a light copy and a dark copy, each shown only in its scheme.
   Run on the finished elements (classes on, the page's declarations stripped), so a copy carries
   nothing the original no longer has. Inside a copied subtree every split descendant takes its
   own scheme's attributes, so the dark copy is dark all the way down. */
function splitBySchemes(splits: Map<Element, Split>) {
  const nested = (element: Element) => {
    for (let parent = element.parentElement; parent; parent = parent.parentElement) if (splits.has(parent)) return true;
    return false;
  };
  const apply = (target: Element, attributes: [string, string | null][]) => {
    for (const [name, value] of attributes) value === null ? target.removeAttribute(name) : target.setAttribute(name, value);
  };
  for (const element of splits.keys()) {
    if (nested(element)) continue;
    const copy = element.cloneNode(true) as Element;
    const copies = [copy, ...Array.from(copy.querySelectorAll("*"))];
    [element, ...Array.from(element.querySelectorAll("*"))].forEach((node, i) => {
      const split = splits.get(node);
      if (split) { apply(node, split.light); apply(copies[i], split.dark); }
    });
    for (const node of copies) node.removeAttribute("id");
    element.classList.add("brindle-light");
    copy.classList.add("brindle-dark");
    element.after(copy);
  }
}

/* One class per distinct set of declarations ([light, dark] per part: the element, ::before, ::after). */
function classFor(classes: Map<string, string>, prefix: string, parts: [string, string][], light: string[], dark: string[]): string {
  const key = JSON.stringify(parts);
  let name = classes.get(key);
  if (!name) {
    name = `${prefix}${classes.size}`;
    classes.set(key, name);
    parts.forEach(([lit, dim], i) => {
      const selector = `.${name}${["", "::before", "::after"][i]}`;
      if (lit) light.push(`${selector}{${lit}}`);
      if (dim) dark.push(`${selector}{${dim}}`);
    });
  }
  return name;
}

const sheetText = (light: string[], dark: string[]) => light.join("") + (dark.length ? `@media (prefers-color-scheme: dark){${dark.join("")}}` : "");

type Width = "narrow" | "wide" | "viewed";

/* Write each element's computed presentation and layout as literal classes (light, then the dark
   differences), remove the page's own declarations of those properties, and return the document's
   HTML.

   Sized values (margins, padding, offsets, grid columns) keep the page's own declarations, which
   still work at any width. To know where those don't survive the sanitiser (a selector or a var()
   it refuses), the export is rehearsed: with only what visual_export.py keeps, plus the classes
   written here, each sized value is measured again, in both schemes and (with `resize`) at a
   narrower and a wider frame, and wherever the rehearsal differs from the page the viewed value
   is written as a literal, per scheme, above anything the page kept. */
export function inlineStyles(doc: Document, read: StyleReader = (element, pseudo) => doc.defaultView!.getComputedStyle(element, pseudo),
  resize?: (width: Width) => void): string {
  const elements = [doc.documentElement, ...(doc.body ? [doc.body, ...Array.from(doc.body.querySelectorAll("*"))] : [])]
    .filter((element) => !NOT_STYLED.has(element.localName));
  const styles = Array.from(doc.querySelectorAll("style"));
  const seen = {} as Record<ColorScheme, Captured[][]>;
  for (const scheme of ["light", "dark"] as const) {
    const undo = forceScheme(doc, scheme);
    try { seen[scheme] = elements.map((element) => capture(element, read)); } finally { undo(); }
  }
  const probes: [ColorScheme, Width][] = [["light", "viewed"], ["dark", "viewed"]];
  if (resize) probes.push(["light", "narrow"], ["light", "wide"]);
  const measure = () => probes.map(([scheme, width]) => {
    const undo = forceScheme(doc, scheme);
    try {
      if (width !== "viewed") resize!(width);
      try { return elements.map((element) => laidOut(element) ? values(read(element), SIZED) : null); } finally { if (width !== "viewed") resize!("viewed"); }
    } finally { undo(); }
  });
  const page = measure();

  const classes = new Map<string, string>();
  const light: string[] = [];
  const dark: string[] = [];
  const splits = new Map<Element, Split>();
  const unsized = (captured: Captured) => Object.fromEntries(Object.entries(captured).filter(([name]) => !SIZED.includes(name)));
  elements.forEach((element, index) => {
    const parts = seen.light[index].map((part, i) => [unsized(part), unsized(seen.dark[index][i])] as const);
    const split = element.namespaceURI === SVG ? settleReferences(element, parts[0][0], parts[0][1]) : null;
    if (split) splits.set(element, split);
    element.classList.add(classFor(classes, "brindle-s", parts.map(([lit, dim]) => declarations(element.localName, lit, dim)), light, dark));
  });
  if (splits.size) {
    light.push(".brindle-dark{display:none}");
    dark.push(".brindle-light{display:none}.brindle-dark{display:inline}");
  }
  for (const sheet of styles) {
    try {
      if (!sheet.sheet) continue;
      stripRules(sheet.sheet.cssRules);
      sheet.textContent = Array.from(sheet.sheet.cssRules).map((rule) => rule.cssText).join("\n");
    } catch { sheet.remove(); }
  }
  for (const element of elements) {
    strip((element as HTMLElement).style);
    if (element.getAttribute("style")?.trim() === "") element.removeAttribute("style");
  }
  const generated = doc.createElement("style");
  generated.textContent = sheetText(light, dark);
  (doc.head ?? doc.documentElement).append(generated);

  // The rehearsal: only what the sanitiser keeps of the page's CSS and inline styles.
  const kept = styles.map((sheet) => sheet.textContent);
  const inline = elements.map((element) => element.getAttribute("style"));
  for (const sheet of styles) sheet.textContent = sheet.isConnected && sheet.sheet ? keptCss(sheet.sheet.cssRules) : "";
  for (const element of elements) {
    const declared = (element as HTMLElement).style ? keptDeclarations((element as HTMLElement).style) : "";
    if (declared) element.setAttribute("style", declared);
    else element.removeAttribute("style");
  }
  let exported: (Captured | null)[][];
  try { exported = measure(); } finally {
    styles.forEach((sheet, i) => { sheet.textContent = kept[i]; });
    elements.forEach((element, i) => { const style = inline[i]; if (style === null) element.removeAttribute("style"); else element.setAttribute("style", style); });
  }
  const literals = new Map<string, string>();
  elements.forEach((element, index) => {
    const differs = SIZED.filter((name) => probes.some((_, p) => page[p][index]?.[name] !== exported[p][index]?.[name]));
    if (!differs.length) return;
    const [lit, dim] = [page[0][index]!, page[1][index]!];
    const declared = (from: Captured, names: string[]) => names.map((name) => `${name}:${from[name]} !important`).join(";");
    const parts: [string, string][] = [[declared(lit, differs.filter((name) => lit[name] && keptValue(lit[name]))),
      declared(dim, differs.filter((name) => dim[name] && dim[name] !== lit[name] && keptValue(dim[name])))]];
    element.classList.add(classFor(literals, "brindle-z", parts, light, dark));
  });
  splitBySchemes(splits);
  generated.textContent = sheetText(light, dark);
  return doc.documentElement.outerHTML;
}

const INERT_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'";
const ACTIVE = "script, iframe, frame, frameset, object, embed, applet, link, meta, base, portal, template, audio, video, source, track";
const FETCHING = new Set(["src", "srcset", "href", "xlink:href", "action", "formaction", "poster", "background", "data", "ping",
  "longdesc", "lowsrc", "dynsrc", "manifest", "codebase", "archive"]);

/* The snapshot as a document that can run nothing and fetch nothing: no active elements, no event
   handlers, no reference but data: and the page's own #ids, under a policy that refuses the rest. */
export function inertSource(html: string): { source: string; empty: boolean } {
  const parsed = new DOMParser().parseFromString(html, "text/html");
  for (const element of Array.from(parsed.querySelectorAll(ACTIVE))) element.remove();
  for (const element of Array.from(parsed.querySelectorAll("*"))) {
    for (const attr of Array.from(element.attributes)) {
      const name = attr.name.toLowerCase();
      if (name.startsWith("on") || (FETCHING.has(name) && !/^\s*(?:#|data:)/i.test(attr.value))) element.removeAttribute(attr.name);
    }
  }
  const policy = parsed.createElement("meta");
  policy.httpEquiv = "Content-Security-Policy";
  policy.content = INERT_CSP;
  parsed.head.prepend(policy);
  return { source: `<!doctype html>${parsed.documentElement.outerHTML}`, empty: parsed.body.childNodes.length === 0 };
}

/* Render the snapshot inert in a hidden, script-free frame of `size` and inline its computed
   presentation. If the frame can't render it, the snapshot goes as it came (the sanitiser still
   makes it safe; it just loses the variables). */
export async function computedSnapshot(html: string, size: { width: number; height: number }): Promise<string> {
  const { source, empty } = inertSource(html);
  const width = Math.max(320, Math.round(size.width) || 1024);
  const height = Math.max(240, Math.round(size.height) || 768);
  const frame = document.createElement("iframe");
  frame.setAttribute("sandbox", "allow-same-origin");
  frame.setAttribute("aria-hidden", "true");
  frame.tabIndex = -1;
  frame.style.cssText = `position:fixed;left:${-width - 100}px;top:0;width:${width}px;height:${height}px;border:0;visibility:hidden;pointer-events:none`;
  const loaded = new Promise<void>((resolve) => frame.addEventListener("load", () => resolve(), { once: true }));
  frame.srcdoc = source;
  document.body.append(frame);
  try {
    await loaded;
    const doc = frame.contentDocument;
    if (!doc?.body || (doc.body.childNodes.length === 0 && !empty)) return html;
    // Far enough either side that a centred column's auto margins or a share of the width can't stay put.
    const widths = { narrow: Math.min(Math.round(0.6 * width), 640), wide: Math.max(2 * width, 2400), viewed: width };
    return inlineStyles(doc, undefined, (to) => { frame.style.width = `${widths[to]}px`; });
  } catch {
    return html;
  } finally {
    frame.remove();
  }
}

export interface VisualFrameProps {
  source: VisualSource;
  title: string;
  /* An increment asks the shell to post the current rendered outerHTML. */
  snapshotRequest?: number;
  /* A menu is open over the page: the native page steps aside (no room) so the menu shows. */
  covered?: boolean;
  /* A message the page posted, and the visual the host saw it come from. */
  onMessage(data: unknown, from: VisualSource): void;
}

/* Browser development: the frame's place. It loads nothing. */
export function PlaceholderFrame({ source, title }: VisualFrameProps) {
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center p-s3 text-center text-sm text-ink-3" data-testid="visual-frame" data-src={visualSrc(source)} aria-label={title}>
      This visual shows here once the sandboxed view is in this build.
    </div>
  );
}

export const VisualFrame: ComponentType<VisualFrameProps> = (props) => isTauri() ? <NativeFrame {...props} /> : <PlaceholderFrame {...props} />;
