import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import type { VisualDTO } from "../../../api/client";
import { inertSource } from "./frame";

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
    for (const group of visual.choices ?? []) {
      let field = Array.from(doc.querySelectorAll("fieldset[data-choice]")).find(el => el.getAttribute("data-choice") === group.id);
      if (!field) {
        field = doc.createElement("fieldset");
        field.setAttribute("data-choice", group.id);
        doc.body.append(field);
      }
      const legend = doc.createElement("legend");
      legend.textContent = group.question;
      field.prepend(legend);
      for (const option of group.options) {
        let item = Array.from(field.querySelectorAll<HTMLButtonElement>("button[data-option]")).find(el => el.dataset.option === option.id);
        if (!item) {
          item = doc.createElement("button");
          item.dataset.option = option.id;
          field.append(item);
        }
        // Keep the preview and explanatory children intact. The app adds the canonical
        // answer label separately so the visible/accessibility label matches delivery.
        const label = doc.createElement("span");
        label.setAttribute("data-choice-label", "");
        label.style.display = "block";
        label.textContent = option.label;
        item.append(label);
        item.setAttribute("aria-label", option.label);
      }
    }
    return `<!doctype html>${doc.documentElement.outerHTML}`;
  }, [visual.inline_html, visual.choices]);
  const refresh = () => {
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
        item.type = "button";
        item.disabled = !!row.answer || !!row.answered_in_chat || !group.options.some(o => o.id === id);
        item.setAttribute("role", "radio");
        const checked = selected[group.id] === id;
        item.setAttribute("aria-checked", String(checked));
        item.style.setProperty("outline", checked ? "3px solid #8055c7" : "none", "important");
      }
    }
  };
  useLayoutEffect(refresh, [picks, visual, zoom]);
  useEffect(() => {
    const doc = frame.current?.contentDocument;
    if (snapshotRequest > 0 && doc) onSnapshot(doc.documentElement.outerHTML);
  }, [snapshotRequest]);
  return <iframe ref={frame} title={visual.title} className="min-h-0 w-full flex-1 border-0"
    sandbox="allow-same-origin" srcDoc={source} onLoad={() => {
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
      doc.onkeydown = event => { if (event.key === "Escape") onBack(); };
      refresh();
    }} />;
}
