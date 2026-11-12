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
const PAINT = ["fill", "fill-opacity", "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap", "stroke-linejoin",
  "stroke-opacity", "text-anchor"];
const STOP = ["stop-color", "stop-opacity"];
const INITIAL: Record<string, string[]> = {
  "background-color": ["rgba(0, 0, 0, 0)", "transparent"], "background-image": ["none"], "box-shadow": ["none"],
  opacity: ["1"], "text-decoration-line": ["none"], ...Object.fromEntries(CORNERS.map((corner) => [`border-${corner}-radius`, ["0px"]])),
};
/* The user agent styles these itself, so their box values are always written. */
const UA_BOXED = new Set(["a", "u", "s", "ins", "del", "hr", "mark", "abbr"]);
/* The page's own declarations that the captured values replace (shorthands included). */
const REPLACED = new Set([...TEXT, ...BOX, ...PAINT, ...STOP, "content", "background", "border", "border-color", "border-style", "border-width",
  "border-radius", ...SIDES.map((side) => `border-${side}`), "font", "text-decoration", "text-decoration-color", "text-decoration-style"]);
const NOT_STYLED = new Set(["head", "style", "script", "template", "noscript"]);
const FETCHES = /url\s*\(|image-set|image\(|\\|</i;

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

function capture(element: Element, read: StyleReader): Captured[] {
  const paint = element.namespaceURI !== SVG ? [] : element.localName === "stop" ? STOP : PAINT;
  const names = [...TEXT, ...BOX, ...paint];
  const values = (style: ReturnType<StyleReader>, list: string[]) => Object.fromEntries(list.map((name) => [name, style.getPropertyValue(name).trim()]));
  const parts = [values(read(element), names)];
  for (const pseudo of ["::before", "::after"]) {
    const style = read(element, pseudo);
    const content = style.getPropertyValue("content").trim();
    parts.push(/^"[^"\\]*"$/.test(content) ? { content, ...values(style, [...TEXT, "background-color"]) } : {});
  }
  return parts;
}

/* The declarations to write for one element part: every value in light, the differences in dark. */
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
  for (const name of Object.keys(light)) {
    const [l, d] = [light[name], dark[name] ?? light[name]];
    if (omitted.has(name) || !l || !d || FETCHES.test(l) || FETCHES.test(d)) continue;
    if (name === "background-image" && !/gradient\(/.test(l + d)) continue;
    if (!UA_BOXED.has(tag) && INITIAL[name]?.includes(l) && INITIAL[name]?.includes(d)) continue;
    lit.push(`${name}:${l}`);
    if (d !== l) dim.push(`${name}:${d}`);
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

/* Write each element's computed presentation as literal classes (light, then the dark differences),
   remove the page's own declarations of those properties, and return the document's HTML. */
export function inlineStyles(doc: Document, read: StyleReader = (element, pseudo) => doc.defaultView!.getComputedStyle(element, pseudo)): string {
  const elements = [doc.documentElement, ...(doc.body ? [doc.body, ...Array.from(doc.body.querySelectorAll("*"))] : [])]
    .filter((element) => !NOT_STYLED.has(element.localName));
  const seen = {} as Record<ColorScheme, Captured[][]>;
  for (const scheme of ["light", "dark"] as const) {
    const undo = forceScheme(doc, scheme);
    try { seen[scheme] = elements.map((element) => capture(element, read)); } finally { undo(); }
  }
  const classes = new Map<string, string>();
  const light: string[] = [];
  const dark: string[] = [];
  elements.forEach((element, index) => {
    const parts = seen.light[index].map((part, i) => declarations(element.localName, part, seen.dark[index][i]));
    const key = JSON.stringify(parts);
    let name = classes.get(key);
    if (!name) {
      name = `brindle-s${classes.size}`;
      classes.set(key, name);
      parts.forEach(([lit, dim], i) => {
        const selector = `.${name}${["", "::before", "::after"][i]}`;
        if (lit) light.push(`${selector}{${lit}}`);
        if (dim) dark.push(`${selector}{${dim}}`);
      });
    }
    element.classList.add(name);
  });
  for (const sheet of Array.from(doc.querySelectorAll("style"))) {
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
  generated.textContent = light.join("") + (dark.length ? `@media (prefers-color-scheme: dark){${dark.join("")}}` : "");
  (doc.head ?? doc.documentElement).append(generated);
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
    return inlineStyles(doc);
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
