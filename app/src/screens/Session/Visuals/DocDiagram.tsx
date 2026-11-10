/* A design doc's diagram section (PlanReader.tsx). An SVG is drawn in the page, so it takes the
   theme's ink: its source is parsed and rebuilt as React elements (never set as HTML), keeping
   only drawing elements and attributes, the same rule the engine holds the supervisor's SVG to
   (brindle/plan_docs.py), and a reference may only name something in the drawing itself. Its ids
   are prefixed with the section's, so two diagrams can't name each other's parts. Mermaid has no
   renderer in the app yet: its source is shown as written. */

import { createElement, useMemo, type ReactNode } from "react";
import type { PlanSectionDTO } from "../../../api/schema";

const ELEMENTS = new Set(["svg", "g", "defs", "title", "desc", "symbol", "use", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
  "text", "tspan", "textPath", "marker", "linearGradient", "radialGradient", "stop", "clipPath", "mask", "pattern"]);
const camel = (name: string) => (/^(aria|data)-/.test(name) ? name : name.replace(/[-:]([a-z])/g, (_, c: string) => c.toUpperCase()));

function build(el: Element, prefix: string, key: string, root = false): ReactNode {
  if (!ELEMENTS.has(el.localName)) return null;
  const props: Record<string, string> = { key };
  for (const { name, value } of [...el.attributes]) {
    const local = name.replace(/^xlink:/, "");
    if (/^on/i.test(local) || local === "style" || name.startsWith("xmlns") || (root && (local === "width" || local === "height"))) continue;
    if (local === "href") {
      if (value.trim().startsWith("#")) props.href = `#${prefix}${value.trim().slice(1)}`;
    } else if (local === "id") props.id = prefix + value;
    else if (local === "class") continue;
    else props[camel(local)] = value.replace(/url\(\s*(['"]?)#/g, `url($1#${prefix}`);
  }
  if (root) {
    // Sized by the doc's column, not by the drawing's own width and height.
    const [w, h] = [el.getAttribute("width"), el.getAttribute("height")].map((v) => parseFloat(v ?? ""));
    if (!props.viewBox && w > 0 && h > 0) props.viewBox = `0 0 ${w} ${h}`;
    Object.assign(props, { role: "img", className: "block h-auto w-full max-w-[720px] fill-current text-ink-3" });
  }
  const kids = [...el.childNodes].map((node, i) => (node.nodeType === 3 ? node.textContent : node.nodeType === 1 ? build(node as Element, prefix, String(i)) : null));
  return createElement(el.localName, props, ...kids);
}

function drawing(source: string, prefix: string): ReactNode {
  try {
    const root = new DOMParser().parseFromString(source, "image/svg+xml").documentElement;
    return root.localName === "svg" ? build(root, prefix, "svg", true) : null;
  } catch {
    return null;
  }
}

export function Diagram({ section, figure, children }: { section: PlanSectionDTO; figure: number; children?: ReactNode }) {
  const drawn = useMemo(() => (section.kind === "svg" ? drawing(section.markdown, `${section.id}-`) : null), [section.kind, section.markdown, section.id]);
  return (
    <figure className="m-0 grid gap-2" data-diagram={section.id}>
      {drawn ?? <pre className="overflow-x-auto rounded-md bg-surface-2 px-3.5 py-3 font-mono text-[12.5px] leading-[1.6] text-ink-2">{section.markdown}</pre>}
      <figcaption className="flex flex-wrap items-center gap-x-2 font-sans text-xs text-ink-3">
        <span>{`Figure ${figure}${section.caption ? ` · ${section.caption}` : ""}`}</span>
        {children}
      </figcaption>
    </figure>
  );
}
