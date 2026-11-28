import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import type { VisualDTO } from "../../../api/client";
import { inertSource } from "./frame";

function choiceButtons(field: Element) {
  return Array.from(field.querySelectorAll<HTMLButtonElement>("button[data-option]"))
    .filter(button => button.closest("fieldset[data-choice]") === field);
}

function resetOptionAppearance(button: HTMLButtonElement) {
  const { style } = button;
  // Remove competing inline shorthands and logical properties before setting the app's styles.
  for (const property of Array.from(style)) {
    if (/^(background|border|outline|color|box-shadow)(-|$)/.test(property)) {
      style.removeProperty(property);
    }
  }
  // Pair CanvasText with an opaque Canvas even when the page uses a custom background.
  style.setProperty("background-color", "Canvas", "important");
  style.setProperty("background-image", "none", "important");
  style.setProperty("background-clip", "border-box", "important");
  style.setProperty("border", "1px solid GrayText", "important");
  style.setProperty("border-image", "none", "important");
  style.setProperty("border-radius", "4px", "important");
  style.setProperty("box-shadow", "none", "important");
  style.setProperty("outline", "revert", "important"); // Keep the browser's keyboard focus indicator.
  style.setProperty("outline-offset", "revert", "important");
  style.setProperty("color", "CanvasText", "important");
  style.setProperty("transition-property", "none", "important");
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
    // Reserve the state hooks for the choice controls the app inserts below.
    for (const el of doc.querySelectorAll("fieldset[data-choice] :is([data-choice-mark], [data-choice-tick], [data-choice-status], [data-choice-dot], [data-needs-answer], [data-choice-state], button[data-option][data-selected])")) {
      el.removeAttribute("data-choice-mark");
      el.removeAttribute("data-choice-tick");
      el.removeAttribute("data-choice-status");
      el.removeAttribute("data-choice-dot");
      el.removeAttribute("data-needs-answer");
      el.removeAttribute("data-choice-state");
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
      status.style.cssText = `
        all: initial !important; display: none !important; align-items: center !important; gap: 6px !important;
        margin-inline-start: 12px !important; vertical-align: 1px !important; white-space: nowrap !important;
        font-family: inherit !important; font-size: 12px !important; font-weight: 500 !important; line-height: 1.4 !important;
      `;
      const dot = doc.createElement("span");
      dot.setAttribute("data-choice-dot", "");
      dot.setAttribute("aria-hidden", "true");
      dot.style.cssText = `
        all: initial !important; display: inline-block !important; box-sizing: border-box !important;
        flex: none !important; width: 7px !important; height: 7px !important; border-radius: 50% !important;
      `;
      status.append(dot, "Needs an answer");
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
          all: initial !important; display: inline-block !important; position: relative !important; box-sizing: border-box !important;
          width: 15px !important; height: 15px !important; flex: none !important;
          margin-inline-end: 9px !important; vertical-align: -2.5px !important; border-radius: 50% !important;
        `;
        const tick = doc.createElement("span");
        tick.setAttribute("data-choice-tick", "");
        tick.style.cssText = `
          all: initial !important; display: block !important; position: absolute !important; box-sizing: content-box !important;
          left: 5px !important; top: 2.5px !important; width: 4px !important; height: 8px !important;
          border-style: solid !important; border-width: 0 1.75px 1.75px 0 !important;
          transform: rotate(45deg) !important;
        `;
        mark.append(tick);
        item.prepend(mark);
      }
    }
    for (const button of doc.querySelectorAll<HTMLButtonElement>("fieldset[data-choice] button[data-option]")) {
      resetOptionAppearance(button);
    }
    return `<!doctype html>${doc.documentElement.outerHTML}`;
  }, [visual.inline_html, visual.choices]);
  const refresh = (loaded = false) => {
    const doc = frame.current?.contentDocument;
    if (!doc?.body) return;
    doc.documentElement.style.setProperty("zoom", String(current.current.zoom));
    const theme = getComputedStyle(document.documentElement);
    const token = (name: string, fallback: string) => theme.getPropertyValue(name).trim() || fallback;
    const ground = token("--canvas", "Canvas");
    const ink = token("--ink", "CanvasText");
    const muted = token("--ink-3", "GrayText");
    const line = token("--line", "GrayText");
    const ring = token("--line-strong", line);
    const pass = token("--pass", "Highlight");
    const passSoft = token("--pass-soft", "color-mix(in srgb, Highlight 12%, transparent)");
    const passLine = `color-mix(in srgb, ${pass} 40%, transparent)`;
    const wash = `color-mix(in srgb, ${pass} 7%, transparent)`;
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
        status.style.setProperty("display", needsAnswer ? "inline-flex" : "none", "important");
        status.style.setProperty("color", muted, "important");
        status.querySelector<HTMLElement>(":scope > [data-choice-dot]")?.style
          .setProperty("border", `1.5px solid ${muted}`, "important");
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
        if (checked) item.dataset.choiceState = "soft-tint";
        else delete item.dataset.choiceState;
        item.style.setProperty("background-color", ground, "important");
        item.style.setProperty("border-color", checked ? passLine : line, "important");
        item.style.setProperty("background-image", checked ? `linear-gradient(${wash}, ${wash})` : "none", "important");
        item.style.setProperty("color", item.disabled && !checked ? muted : ink, "important");
        const mark = item.querySelector<HTMLElement>(":scope > [data-choice-mark]");
        if (mark) {
          mark.dataset.choiceMark = checked ? "selected" : "empty";
          mark.style.setProperty("background-color", checked ? passSoft : "transparent", "important");
          mark.style.setProperty("box-shadow", checked ? "none" : `inset 0 0 0 1.5px ${ring}`, "important");
          mark.querySelector<HTMLElement>(":scope > [data-choice-tick]")?.style
            .setProperty("border-color", checked ? pass : "transparent", "important");
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
    const sync = () => refresh();
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
      refresh(true);
    }} />;
}
