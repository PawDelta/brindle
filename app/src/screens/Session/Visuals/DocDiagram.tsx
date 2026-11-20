/* A design doc's diagram section (PlanReader.tsx). An SVG is drawn in the page, so it takes the
   theme's ink: its source is parsed and rebuilt as React elements (never set as HTML), keeping
   only drawing elements and attributes, the same rule the engine holds the supervisor's SVG to
   (brindle/plan_docs.py), and a reference may only name something in the drawing itself. Its ids
   are prefixed with the section's, so two diagrams can't name each other's parts. A mermaid
   section is drawn by mermaid (loaded on first use, in strict mode, with HTML labels off) in the
   app's light or dark tokens; if it can't be drawn, its source is shown as written. */

import { createElement, useEffect, useMemo, useState, type ReactNode } from "react";
import type { PlanSectionDTO } from "../../../api/schema";
import { useAppTheme } from "../../../components/useAppTheme";

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

/* mermaid is loaded the first time a doc has a mermaid section, and shared after that. */
type Mermaid = typeof import("mermaid").default;
let mermaidLoad: Promise<Mermaid> | null = null;
const loadMermaid = () => (mermaidLoad ??= import("mermaid").then((m) => m.default));
let drawings = 0;

/* The app's tokens, read from the page as it is showing now (light or dark). */
const token = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
function mermaidTheme() {
  const surface = token("--surface-2");
  const ink = token("--ink");
  const line = token("--line-strong");
  return {
    background: token("--canvas"), mainBkg: surface, secondaryColor: surface, tertiaryColor: surface, clusterBkg: surface,
    primaryColor: surface, primaryTextColor: ink, primaryBorderColor: line, secondaryTextColor: ink, tertiaryTextColor: ink,
    lineColor: token("--ink-3"), textColor: ink, edgeLabelBackground: token("--canvas"), fontFamily: "inherit",
  };
}

/* A mermaid drawing: the svg mermaid makes (strict, so it carries no script or foreign content), or
   a failure, which the figure answers by showing the source. A new theme or source draws again. */
function MermaidDrawing({ source, onFail }: { source: string; onFail(): void }) {
  const theme = useAppTheme();
  const [svg, setSvg] = useState<string | null>(null);
  useEffect(() => {
    let current = true;
    setSvg(null);
    const draw = async () => {
      try {
        const mermaid = await loadMermaid();
        mermaid.initialize({ startOnLoad: false, securityLevel: "strict", suppressErrorRendering: true, htmlLabels: false, flowchart: { htmlLabels: false }, theme: "base", themeVariables: mermaidTheme() });
        const id = `doc-mermaid-${++drawings}`;
        try {
          const { svg: out } = await mermaid.render(id, source);
          if (current) setSvg(out);
        } catch (error) {
          // mermaid's temporary container is in the page body, not the figure: take it out if it's still there.
          document.getElementById(`d${id}`)?.remove();
          document.getElementById(id)?.remove();
          throw error;
        }
      } catch {
        if (current) onFail();
      }
    };
    void draw();
    return () => {
      current = false;
    };
    // The theme is a redraw trigger only; onFail is the parent's state setter.
  }, [source, theme]);
  return svg === null ? <div aria-busy="true" className="h-24" /> : <div role="img" aria-label="Diagram" className="max-w-[720px] [&_svg]:h-auto [&_svg]:max-w-full" dangerouslySetInnerHTML={{ __html: svg }} />;
}

export function Diagram({ section, figure, children }: { section: PlanSectionDTO; figure: number; children?: ReactNode }) {
  const drawn = useMemo(() => (section.kind === "svg" ? drawing(section.markdown, `${section.id}-`) : null), [section.kind, section.markdown, section.id]);
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [section.markdown]);
  const mermaid = section.kind === "mermaid" && !failed;
  return (
    <figure className="m-0 grid gap-2" data-diagram={section.id}>
      {mermaid ? <MermaidDrawing source={section.markdown} onFail={() => setFailed(true)} />
        : drawn ?? <pre className="overflow-x-auto rounded-md bg-surface-2 px-3.5 py-3 font-mono text-[12.5px] leading-[1.6] text-ink-2">{section.markdown}</pre>}
      <figcaption className="flex flex-wrap items-center gap-x-2 font-sans text-xs text-ink-3">
        <span>{`Figure ${figure}${section.caption ? ` · ${section.caption}` : ""}`}</span>
        {failed && <span className="text-ink-3">· couldn't draw this diagram</span>}
        {children}
      </figcaption>
    </figure>
  );
}
