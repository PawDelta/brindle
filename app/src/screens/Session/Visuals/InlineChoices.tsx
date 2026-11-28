import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import type { VisualDTO } from "../../../api/client";
import { inertSource } from "./frame";

const choiceAccent = "var(--brindle-choice-border, Highlight)";
const choiceNeutral = "var(--brindle-choice-neutral, GrayText)";

function choiceButtons(field: Element) {
  return Array.from(field.querySelectorAll<HTMLButtonElement>("button[data-option]"))
    .filter(button => button.closest("fieldset[data-choice]") === field);
}

function syncChoiceColors(doc: Document) {
  const theme = getComputedStyle(document.documentElement);
  doc.documentElement.style.setProperty("--brindle-choice-border", theme.getPropertyValue("--branch"));
  doc.documentElement.style.setProperty("--brindle-choice-fill", theme.getPropertyValue("--branch-soft"));
  doc.documentElement.style.setProperty("--brindle-choice-neutral", theme.getPropertyValue("--ink-3"));
  doc.documentElement.style.setProperty("--brindle-choice-required", theme.getPropertyValue("--sap"));
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
  const originalOptionStyles = useRef(new WeakMap<HTMLButtonElement, string | null>());
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
    // Reserve the state hooks for the choice controls the app inserts below.
    for (const el of doc.querySelectorAll("fieldset[data-choice] :is([data-choice-mark], [data-choice-status], [data-needs-answer], button[data-option][data-selected])")) {
      el.removeAttribute("data-choice-mark");
      el.removeAttribute("data-choice-status");
      el.removeAttribute("data-needs-answer");
      if (el.matches("button[data-option]")) el.removeAttribute("data-selected");
    }
    for (const group of visual.choices ?? []) {
      let field = Array.from(doc.querySelectorAll("fieldset[data-choice]")).find(el => el.getAttribute("data-choice") === group.id);
      if (!field) {
        field = doc.createElement("fieldset");
        field.setAttribute("data-choice", group.id);
        doc.body.append(field);
      }
      let heading = field.querySelector(":scope > legend") ??
        Array.from(field.querySelectorAll("h1, h2, h3, h4, h5, h6, [role='heading']"))
          .find(el => el.closest("fieldset[data-choice]") === field);
      if (!heading) {
        heading = doc.createElement("legend");
        heading.textContent = group.question;
        field.prepend(heading);
      }
      const status = doc.createElement("span");
      status.setAttribute("data-choice-status", "");
      status.textContent = "Needs an answer";
      status.style.cssText = `
        all: initial !important; display: none !important; margin-inline-start: .65em !important;
        padding: .1em .4em !important; border: 1px solid currentColor !important;
        border-radius: .3em !important; font: 600 .75em/1.4 system-ui !important;
        color: var(--brindle-choice-required, CanvasText) !important; background: Canvas !important;
      `;
      heading.append(status);
      const buttons = choiceButtons(field);
      for (const option of group.options) {
        let item = buttons.find(el => el.dataset.option === option.id);
        if (!item) {
          item = doc.createElement("button");
          item.dataset.option = option.id;
          field.append(item);
        }
        item.setAttribute("aria-label", option.label);
        const mark = doc.createElement("span");
        mark.setAttribute("data-choice-mark", "empty");
        mark.setAttribute("aria-hidden", "true");
        // Inline priority keeps page rules for button children from changing the radio mark.
        mark.style.cssText = `
          all: initial !important; display: inline-block !important; box-sizing: border-box !important;
          width: 1em !important; height: 1em !important; flex: none !important;
          margin-inline-end: .5em !important; vertical-align: -.12em !important;
          font-size: 1em !important; color: transparent !important;
          border: 2px solid ${choiceNeutral} !important; border-radius: 50% !important;
        `;
        item.prepend(mark);
      }
    }
    return `<!doctype html>${doc.documentElement.outerHTML}`;
  }, [visual.inline_html, visual.choices]);
  const refresh = (loaded = false) => {
    const doc = frame.current?.contentDocument;
    if (!doc?.body) return;
    doc.documentElement.style.setProperty("zoom", String(current.current.zoom));
    const { visual: row, picks: selected } = current.current;
    const closed = !!row.answer || !!row.answered_in_chat;
    for (const group of row.choices ?? []) {
      const field = Array.from(doc.querySelectorAll("fieldset[data-choice]")).find(el => el.getAttribute("data-choice") === group.id);
      if (!field) continue;
      field.setAttribute("role", "radiogroup");
      field.setAttribute("aria-label", group.question);
      const pick = selected[group.id];
      const status = Array.from(field.querySelectorAll<HTMLElement>("[data-choice-status]"))
        .find(el => el.closest("fieldset[data-choice]") === field);
      if (status) {
        const needsAnswer = !closed && !group.options.some(option => option.id === pick);
        status.parentElement?.toggleAttribute("data-needs-answer", needsAnswer);
        status.style.setProperty("display", needsAnswer ? "inline-block" : "none", "important");
      }
      for (const item of choiceButtons(field)) {
        const id = item.dataset.option!;
        const option = group.options.find(o => o.id === id);
        item.type = "button";
        item.disabled = closed || !option;
        item.setAttribute("role", "radio");
        const checked = !!option && pick === id;
        item.setAttribute("aria-checked", String(checked));
        item.toggleAttribute("data-selected", checked);
        const originals = originalOptionStyles.current;
        if (checked && !originals.has(item)) {
          // Restore the author's inline declarations when the pick moves to another button.
          originals.set(item, item.getAttribute("style"));
          item.style.setProperty("border-width", "2px", "important");
          item.style.setProperty("border-style", "solid", "important");
          item.style.setProperty("border-color", choiceAccent, "important");
          item.style.setProperty("border-image-source", "none", "important");
          item.style.setProperty("background-color", "var(--brindle-choice-fill)", "important");
        } else if (!checked && originals.has(item)) {
          const original = originals.get(item);
          if (original == null) item.removeAttribute("style");
          else item.setAttribute("style", original);
          originals.delete(item);
        }
        const mark = item.querySelector<HTMLElement>(":scope > [data-choice-mark]");
        if (mark) {
          mark.dataset.choiceMark = checked ? "selected" : "empty";
          mark.style.setProperty("border-color", checked ? choiceAccent : choiceNeutral, "important");
          mark.style.setProperty("background-color", checked ? choiceAccent : "Canvas", "important");
        }
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
