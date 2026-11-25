import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import type { VisualDTO } from "../../../api/client";
import { inertSource } from "./frame";

function syncChoiceColors(doc: Document) {
  const theme = getComputedStyle(document.documentElement);
  doc.documentElement.style.setProperty("--brindle-choice-border", theme.getPropertyValue("--branch"));
  doc.documentElement.style.setProperty("--brindle-choice-fill", theme.getPropertyValue("--branch-soft"));
}

/* No page code runs in this frame. Only the parent app attaches listeners and owns picks.
   Never accept postMessage choices from the scripted native visual for these groups. */
export function InlineChoices({ visual, picks, onPick, zoom, snapshotRequest, onSnapshot, onBack }: {
  visual: VisualDTO;
  picks: Record<string, string>;
  onPick(group: string, option: string): void;
  zoom: number;
  snapshotRequest: number;
  onSnapshot(html: string): void;
  onBack(): void;
}) {
  const frame = useRef<HTMLIFrameElement>(null);
  const current = useRef({ visual, picks, onPick, zoom });
  current.current = { visual, picks, onPick, zoom };
  const source = useMemo(() => {
    const doc = new DOMParser().parseFromString(inertSource(visual.inline_html ?? "").source, "text/html");
    // Keep the frame on this CSP-protected document. Links in an inline visual are inert.
    for (const link of doc.querySelectorAll("a, area, math, math *")) {
      link.removeAttribute("href");
      link.removeAttribute("xlink:href");
    }
    for (const animation of doc.querySelectorAll("svg animate, svg set")) {
      const attribute = animation.getAttribute("attributeName");
      if (attribute === "href" || attribute?.endsWith(":href")) animation.remove();
    }
    for (const group of visual.choices ?? []) {
      let field = Array.from(doc.querySelectorAll("fieldset[data-choice]")).find(el => el.getAttribute("data-choice") === group.id);
      if (!field) {
        field = doc.createElement("fieldset");
        field.setAttribute("data-choice", group.id);
        doc.body.append(field);
      }
      if (!field.querySelector(":scope > legend, h1, h2, h3, h4, h5, h6, [role='heading']")) {
        const legend = doc.createElement("legend");
        legend.textContent = group.question;
        field.prepend(legend);
      }
      for (const option of group.options) {
        let item = Array.from(field.querySelectorAll<HTMLButtonElement>("button[data-option]")).find(el => el.dataset.option === option.id);
        if (!item) {
          item = doc.createElement("button");
          item.dataset.option = option.id;
          field.append(item);
        }
        item.setAttribute("aria-label", option.label);
      }
    }
    const style = doc.createElement("style");
    style.textContent = `
      fieldset[data-choice] button[data-option][role="radio"][aria-checked="true"] {
        border: 2px solid !important;
        border-color: var(--brindle-choice-border) !important;
        background-color: var(--brindle-choice-fill) !important;
      }
    `;
    doc.head.append(style);
    return `<!doctype html>${doc.documentElement.outerHTML}`;
  }, [visual.inline_html, visual.choices]);
  const refresh = (loaded = false) => {
    const doc = frame.current?.contentDocument;
    if (!doc?.body) return;
    doc.documentElement.style.setProperty("zoom", String(current.current.zoom));
    const { visual: row, picks: selected } = current.current;
    for (const group of row.choices ?? []) {
      const field = Array.from(doc.querySelectorAll("fieldset[data-choice]")).find(el => el.getAttribute("data-choice") === group.id);
      if (!field) continue;
      field.setAttribute("role", "radiogroup");
      field.setAttribute("aria-label", group.question);
      for (const item of Array.from(field.querySelectorAll<HTMLButtonElement>("button[data-option]"))) {
        const id = item.dataset.option!;
        const option = group.options.find(o => o.id === id);
        item.type = "button";
        item.disabled = !!row.answer || !!row.answered_in_chat || !option;
        item.setAttribute("role", "radio");
        item.setAttribute("aria-checked", String(selected[group.id] === id));
        // Read rendered text after loading so hidden preview text doesn't replace the label.
        if (loaded && option && !(item.innerText ?? item.textContent)?.trim()) {
          const label = doc.createElement("span");
          label.setAttribute("data-choice-label", "");
          label.style.display = "block";
          label.textContent = option.label;
          item.append(label);
        }
      }
    }
  };
  useLayoutEffect(refresh, [picks, visual, zoom]);
  useEffect(() => {
    const sync = () => {
      const doc = frame.current?.contentDocument;
      if (doc) syncChoiceColors(doc);
    };
    const observer = new MutationObserver(sync);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "style", "class"] });
    const system = window.matchMedia?.("(prefers-color-scheme: dark)");
    system?.addEventListener("change", sync);
    return () => {
      observer.disconnect();
      system?.removeEventListener("change", sync);
    };
  }, []);
  useEffect(() => {
    const doc = frame.current?.contentDocument;
    if (snapshotRequest > 0 && doc) onSnapshot(doc.documentElement.outerHTML);
  }, [snapshotRequest]);
  // Older WebKit blocks even the parent's listeners with the sandboxed scripts flag
  // (WebKit bug 98951). The sanitized srcdoc's CSP forbids all document scripts.
  return <iframe ref={frame} title={visual.title} className="min-h-0 w-full flex-1 border-0"
    sandbox="allow-same-origin allow-scripts" srcDoc={source} onLoad={() => {
      const doc = frame.current?.contentDocument;
      if (!doc) return;
      // Cancel links/forms too: this document remains the inert revision we loaded.
      doc.onclick = event => {
        event.preventDefault();
        const target = event.target as Element | null;
        const option = target?.closest?.("button[data-option]");
        const group = option?.closest("fieldset[data-choice]")?.getAttribute("data-choice");
        const id = option?.getAttribute("data-option");
        const row = current.current.visual;
        if (!row.answer && !row.answered_in_chat && group && id && row.choices?.some(g => g.id === group && g.options.some(o => o.id === id))) {
          current.current.onPick(group, id);
        }
      };
      doc.onsubmit = event => event.preventDefault();
      doc.onkeydown = event => {
        if (event.key !== "Escape" || event.defaultPrevented) return;
        event.preventDefault();
        event.stopPropagation();
        onBack();
      };
      syncChoiceColors(doc);
      refresh(true);
    }} />;
}
