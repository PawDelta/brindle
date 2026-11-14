/* One section of a shared design doc, edited live with everyone else writing in it (PlanReader's
   Editing mode on a doc in team review). The text is the section's LoroText in the visual's one
   Loro document, which the daemon holds and relays over DaemonClient.visualLive: this editor
   imports the document as the daemon has it, sends each local change, and draws the changes
   others send as they arrive, with each writer's caret and name where they are (loro-codemirror's
   ephemeral plugin, one EphemeralStore per section). Undo takes back this person's own changes
   only (Loro's UndoManager), never a teammate's.

   CodeMirror and Loro's JS API both count text in UTF-16 units, so a change goes across as is;
   the engine's code points (DocText.tsx toPoints) only matter where the text becomes a revision,
   which is the daemon's.

   Until the daemon's first snapshot arrives nothing can be typed: a client never seeds the text
   itself (two would each insert it). When the doc is co-edited through the server's relay, the
   daemon lends this window a number of its own to write under ({"peer"}), set right after the
   snapshot; until one is free the text is read only. When the channel closes the text stays on
   screen, read only, unless the daemon replaced the text (LIVE_RELOAD): then it opens again. */

import { Annotation, Compartment, EditorState, type Extension, type Range } from "@codemirror/state";
import { Decoration, EditorView, ViewPlugin, WidgetType, keymap, type DecorationSet, type ViewUpdate } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { EphemeralStore, LoroDoc, UndoManager } from "loro-crdt";
import { LoroEphemeralPlugin, getCursorEphemeralKey, getUserEphemeralKey, type EphemeralState, type UserState } from "loro-codemirror";
import { useEffect, useRef, useState } from "react";
import { LIVE_RELOAD, type DaemonClient } from "../../../api/client";
import { formatEdit, useFormatTarget, type FormatCommand } from "./DocFormat";
import { changeBetween, toUnits } from "./DocText";
import { PEER_COLOURS, PRESENCE_TTL, peerClass } from "./docAwareness";

export type LiveClient = Pick<DaemonClient, "visualLive">;

/* A change that came from Loro (another writer's, or an undo): already in the document. */
const fromLoro = Annotation.define<boolean>();

/* The section's text kept in step both ways: the view's changes into Loro, and Loro's changes to
   this section (imported, or undone) into the view. Changes to other sections are passed over. */
function loroSync(doc: LoroDoc, section: string): Extension {
  return ViewPlugin.define((view) => {
    const text = doc.getText(section);
    const off = doc.subscribe((batch) => {
      if (batch.by === "local" && batch.origin !== "undo") return;
      for (const event of batch.events) {
        if (event.target !== text.id || event.diff.type !== "text") continue;
        const changes: { from: number; to?: number; insert?: string }[] = [];
        let at = 0;
        for (const d of event.diff.diff) {
          if (d.insert !== undefined) changes.push({ from: at, insert: d.insert });
          else if (d.delete !== undefined) {
            changes.push({ from: at, to: at + d.delete });
            at += d.delete;
          } else at += d.retain ?? 0;
        }
        view.dispatch({ changes, annotations: fromLoro.of(true) });
      }
    });
    return {
      update(update) {
        let changed = false;
        for (const tr of update.transactions) {
          if (!tr.docChanged || tr.annotation(fromLoro)) continue;
          let shift = 0;
          tr.changes.iterChanges((fromA, toA, _fromB, _toB, inserted) => {
            const insert = inserted.toString();
            if (toA > fromA) text.delete(fromA + shift, toA - fromA);
            if (insert) text.insert(fromA + shift, insert);
            shift += insert.length - (toA - fromA);
          });
          changed = true;
        }
        if (changed) doc.commit();
      },
      destroy: off,
    };
  });
}

/* Each writer's caret in a colour of their own, from the app's group colours. */
const peerTheme = EditorView.theme(
  Object.fromEntries(
    Array.from({ length: PEER_COLOURS }, (_, i) => [
      [`.loro-cursor.loro-peer-${i}, .loro-cursor.loro-peer-${i}::before`, { backgroundColor: `var(--group-${i + 2})`, color: "var(--color-surface, #fff)" }],
      [`.loro-selection.loro-peer-${i}`, { backgroundColor: `var(--group-${i + 2})` }],
    ]).flat(),
  ),
);
/* How the text looks. "page": the doc's own serif type, Markdown's marks hidden except on the
   lines the caret or selection is on, so the section reads as it does when not edited. "source":
   the Markdown as typed, in mono, its marks dimmed (the reader's Markdown view). */
export type EditorVariant = "page" | "source";

const shared = {
  "&.cm-focused": { outline: "none" },
  // A teammate's name sits above the line, never over the text it is next to.
  ".loro-cursor::before": { top: "auto", bottom: "100%", padding: "0 5px", fontFamily: "var(--font-sans, ui-sans-serif, system-ui, sans-serif)", fontSize: "10px", fontWeight: "600", lineHeight: "15px", borderRadius: "3px 3px 3px 0", opacity: "1" },
  ".cm-md-mk": { color: "var(--ink-3)" },
  ".cm-md-b": { fontWeight: "600", color: "var(--ink)" },
  ".cm-md-i": { fontStyle: "italic" },
  ".cm-md-a": { textDecoration: "underline", textDecorationColor: "var(--line-strong)", textUnderlineOffset: "3px" },
};
const pageLook = EditorView.theme({
  ...shared,
  "&": { fontSize: "15px", color: "var(--ink-2)" },
  ".cm-content": { fontFamily: "var(--font-serif, 'Iowan Old Style', Charter, Georgia, serif)", lineHeight: "1.65", padding: "0", caretColor: "var(--ink)" },
  ".cm-line": { padding: "0", maxWidth: "72ch" },
  ".cm-md-h1": { fontSize: "20px", fontWeight: "600", color: "var(--ink)" },
  ".cm-md-h2": { fontSize: "17px", fontWeight: "600", color: "var(--ink)" },
  ".cm-md-h3": { fontFamily: "var(--font-sans, ui-sans-serif, system-ui, sans-serif)", fontSize: "14px", fontWeight: "600", color: "var(--ink)" },
  ".cm-md-quote": { borderLeft: "2px solid var(--sap)", paddingLeft: "14px !important", fontFamily: "var(--font-sans, ui-sans-serif, system-ui, sans-serif)", fontSize: "13.5px" },
  ".cm-md-li": { paddingLeft: "20px !important", textIndent: "-14px" },
  ".cm-md-pre": { fontFamily: "var(--font-mono, ui-monospace, monospace)", fontSize: "12.5px", lineHeight: "1.6", background: "var(--surface-2)", padding: "0 14px !important", color: "var(--ink)" },
  ".cm-md-code": { fontFamily: "var(--font-mono, ui-monospace, monospace)", fontSize: "11.5px", background: "var(--surface-2)", borderRadius: "2px", padding: "0 4px" },
  ".cm-md-bullet": { display: "inline-block", width: "14px", color: "var(--ink-3)" },
});
const sourceLook = EditorView.theme({
  ...shared,
  "&": { fontSize: "13px", color: "var(--ink)" },
  ".cm-content": { fontFamily: "var(--font-mono, ui-monospace, monospace)", lineHeight: "1.75", padding: "0" },
  ".cm-line": { padding: "0" },
  ".cm-md-h1, .cm-md-h2, .cm-md-h3": { fontWeight: "600" },
});

class Bullet extends WidgetType {
  eq() { return true; }
  toDOM() {
    const el = document.createElement("span");
    el.className = "cm-md-bullet";
    el.textContent = "•";
    return el;
  }
}
const bullet = Decoration.replace({ widget: new Bullet() });
const hidden = Decoration.replace({});
const dim = Decoration.mark({ class: "cm-md-mk" });
const INLINE_MARKS: [RegExp, string, number][] = [
  [/\*\*([^*\n]+)\*\*/g, "cm-md-b", 2],
  [/(?<![*\w])\*([^*\s][^*\n]*?)\*(?!\*)/g, "cm-md-i", 1],
  [/`([^`\n]+)`/g, "cm-md-code", 1],
];

/* Markdown's look over its source: headings, lists, quotes, code and inline marks styled; with
   `hide`, the marks themselves are hidden off the lines the selection is on. */
function markdownLook(hide: boolean): Extension {
  const build = (view: EditorView): DecorationSet => {
    const out: Range<Decoration>[] = [];
    const { doc, selection } = view.state;
    const active = (from: number, to: number) => selection.ranges.some((r) => r.from <= to && r.to >= from);
    const marker = (from: number, to: number, on: boolean, replace = hidden) => {
      if (to > from) out.push((hide && !on ? replace : dim).range(from, to));
    };
    let fenced = false;
    for (let n = 1; n <= doc.lines; n++) {
      const line = doc.line(n);
      const text = line.text;
      const on = active(line.from, line.to);
      if (/^\s*```/.test(text)) {
        fenced = !fenced;
        out.push(Decoration.line({ class: "cm-md-pre" }).range(line.from));
        marker(line.from, line.to, on);
        continue;
      }
      if (fenced) {
        out.push(Decoration.line({ class: "cm-md-pre" }).range(line.from));
        continue;
      }
      const heading = /^(#{1,6}) /.exec(text);
      const quote = /^> ?/.exec(text);
      const list = /^(\s*)([-*+]|\d+[.)]) /.exec(text);
      if (heading) {
        out.push(Decoration.line({ class: `cm-md-h${Math.min(3, heading[1].length)}` }).range(line.from));
        marker(line.from, line.from + heading[0].length, on);
      } else if (quote) {
        out.push(Decoration.line({ class: "cm-md-quote" }).range(line.from));
        marker(line.from, line.from + quote[0].length, on);
      } else if (list) {
        out.push(Decoration.line({ class: "cm-md-li" }).range(line.from));
        const at = line.from + list[1].length;
        if (/\d/.test(list[2])) out.push(dim.range(at, at + list[2].length + 1));
        else marker(at, at + list[2].length + 1, on, bullet);
      }
      for (const [pattern, cls, width] of INLINE_MARKS) {
        for (const m of text.matchAll(pattern)) {
          const from = line.from + m.index;
          const to = from + m[0].length;
          out.push(Decoration.mark({ class: cls }).range(from + width, to - width));
          marker(from, from + width, on);
          marker(to - width, to, on);
        }
      }
      for (const m of text.matchAll(/\[([^\]\n]+)\]\(([^)\n]*)\)/g)) {
        const from = line.from + m.index;
        const to = from + m[0].length;
        out.push(Decoration.mark({ class: "cm-md-a" }).range(from + 1, from + 1 + m[1].length));
        marker(from, from + 1, on);
        marker(from + 1 + m[1].length, to, on);
      }
    }
    return Decoration.set(out, true);
  };
  return ViewPlugin.fromClass(class {
    decorations: DecorationSet;
    constructor(view: EditorView) { this.decorations = build(view); }
    update(u: ViewUpdate) {
      if (u.docChanged || u.selectionSet || u.viewportChanged) this.decorations = build(u.view);
    }
  }, { decorations: (v) => v.decorations });
}

export function DocEditor({ client, visual, section, name, label, isAuthor = false, variant = "page" }: {
  client: LiveClient;
  /* The shared doc's visual id, and the section edited (its id, "lede" for the overview). */
  visual: string;
  section: string;
  /* What teammates see beside this person's caret. */
  name: string;
  /* The editor's accessible name, e.g. "Text of Goals". */
  label: string;
  isAuthor?: boolean;
  variant?: EditorVariant;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<"connecting" | "live" | "closed">("connecting");
  // Bumped when the daemon replaces the text (LIVE_RELOAD): a new document and channel.
  const [opened, setOpened] = useState(0);
  // Who is writing is fixed for the editor's life: a new name would be a new caret.
  const named = useRef(name);
  // The formatting toolbar writes into this section while it is the last text focused: a
  // command becomes one CodeMirror change, which goes into the Loro draft as typing does.
  const command = useRef<(c: FormatCommand) => void>(() => {});
  const format = useFormatTarget({ apply: (c) => command.current(c) });
  const focused = useRef(format.focused);
  focused.current = format.focused;
  useEffect(() => {
    const doc = new LoroDoc();
    const presence = new EphemeralStore<EphemeralState>(PRESENCE_TTL);
    const undo = new UndoManager(doc, { mergeInterval: 500 });
    const editable = new Compartment();
    const user: UserState = { name: named.current, colorClassName: peerClass(named.current) };
    let view: EditorView | null = null;
    let gone = false;
    let stopRefresh = () => {};
    let lockedByState = true;
    // The number the daemon lent this window ({"peer"}), set once the snapshot is in: the
    // server's relay refuses a change under any other. Waiting: none free yet, read only.
    let lent: `${number}` | null = null;
    let waitingForPeer = false;
    let loaded = false;
    const locked = () => lockedByState || waitingForPeer;
    const editability = () => [EditorState.readOnly.of(locked()), EditorView.editable.of(!locked())];
    const mount = () => {
      if (!host.current) return null;
      const run = (step: () => boolean) => () => locked() || step() || true;
      return new EditorView({
        parent: host.current,
        state: EditorState.create({
          doc: doc.getText(section).toString(),
          extensions: [
            loroSync(doc, section),
            EditorState.transactionFilter.of((tr) => locked() && tr.docChanged && !tr.annotation(fromLoro) ? [] : tr),
            LoroEphemeralPlugin(doc, presence, user, (d) => d.getText(section)),
            keymap.of([
              { key: "Mod-z", run: run(() => undo.undo()), preventDefault: true },
              { key: "Mod-Shift-z", run: run(() => undo.redo()), preventDefault: true },
              { key: "Mod-y", run: run(() => undo.redo()), preventDefault: true },
              ...defaultKeymap,
            ]),
            EditorView.lineWrapping,
            EditorView.contentAttributes.of({ "aria-label": label, "aria-multiline": "true" }),
            EditorView.domEventHandlers({ focus: () => { focused.current(); } }),
            editable.of(editability()),
            variant === "page" ? pageLook : sourceLook,
            markdownLook(variant === "page"),
            peerTheme,
          ],
        }),
      });
    };
    command.current = (c) => {
      if (!view || locked()) return;
      const { from, to } = view.state.selection.main;
      const e = formatEdit(view.state.doc.toString(), from, to, c);
      view.dispatch({ changes: { from: e.from, to: e.to, insert: e.insert }, selection: { anchor: e.anchor, head: e.head }, userEvent: "input.format" });
      view.focus();
    };
    const channel = client.visualLive(visual, {
      onState(state) {
        lockedByState = state.lock === "sign_off" || (state.lock === "author_offline" && !isAuthor);
        view?.dispatch({ effects: editable.reconfigure(editability()) });
      },
      onPeer(peer) {
        waitingForPeer = peer === null;
        if (peer !== null) {
          lent = peer;
          if (loaded) doc.setPeerId(peer);     // locked until now: nothing was written under another
        }
        view?.dispatch({ effects: editable.reconfigure(editability()) });
      },
      onUpdate(bytes) {
        doc.import(bytes);
        if (!loaded) {
          loaded = true;
          // After the snapshot, so the number goes on from its own earlier changes; before any edit.
          if (lent !== null) doc.setPeerId(lent);
        }
        if (view || gone) return;
        view = mount();
        setState("live");
      },
      onAwareness(at, bytes) {
        if (at === section) presence.apply(bytes);
      },
      onClose(code) {
        if (gone) return;
        if (code === LIVE_RELOAD) {
          setState("connecting");
          setOpened((n) => n + 1);
          return;
        }
        stopRefresh();
        lockedByState = true;
        setState("closed");
        view?.dispatch({ effects: editable.reconfigure([EditorState.readOnly.of(true), EditorView.editable.of(false)]) });
      },
    });
    const offUpdates = doc.subscribeLocalUpdates((bytes) => channel.sendUpdate(bytes));
    const offPresence = presence.subscribeLocalUpdates((bytes) => channel.sendAwareness(section, bytes));
    // Presence lapses after PRESENCE_TTL unless written again, and the plugin writes the name once
    // and the caret only as it moves: while here and connected, both are written again, so an idle
    // writer stays on teammates' screens and someone joining later still learns their name.
    const userKey = getUserEphemeralKey(doc);
    const cursorKey = getCursorEphemeralKey(doc);
    const refresh = () => {
      presence.set(userKey, user);
      const cursor = presence.get(cursorKey);
      if (cursor) presence.set(cursorKey, cursor);
    };
    refresh();
    const refreshing = setInterval(refresh, PRESENCE_TTL / 3);
    stopRefresh = () => clearInterval(refreshing);
    return () => {
      gone = true;
      command.current = () => {};
      stopRefresh();
      // Teammates stop seeing this caret: its removal goes out before the channel closes.
      presence.delete(getCursorEphemeralKey(doc));
      presence.delete(getUserEphemeralKey(doc));
      view?.destroy();
      offUpdates();
      offPresence();
      channel.close();
      presence.destroy();
      undo.free();
    };
    // `label` only names the box: a new one is not a new editor.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, visual, section, isAuthor, opened, variant]);
  return (
    <div className="grid gap-1" data-live={state} data-variant={variant}>
      <div ref={host} data-testid={`doc-editor-${section}`} className="min-h-[1.65em]" />
      {state !== "live" && (
        <p className="text-xs text-ink-3" role="status">
          {state === "connecting" ? "Connecting to the live text…" : "Disconnected: the text is read only until the doc is opened again."}
        </p>
      )}
    </div>
  );
}

/* A suggestion typed on the page (PlanReader's Suggesting): the section's own text, in its page
   look, that only this person changes and nothing is sent until the suggestion is. What differs
   from the text it was opened on is shown tracked: what goes struck through, what comes in green.
   One suggestion is one replaced range (changeBetween), as the engine takes it. */
class Struck extends WidgetType {
  constructor(readonly text: string) { super(); }
  eq(other: Struck) { return other.text === this.text; }
  toDOM() {
    const el = document.createElement("del");
    el.className = "cm-sg-del";
    el.textContent = this.text;
    return el;
  }
}

function tracked(source: string): Extension {
  const build = (view: EditorView): DecorationSet => {
    const change = changeBetween(source, view.state.doc.toString());
    if (!change.before && !change.after) return Decoration.none;
    const at = toUnits(source, change.start);
    const out: Range<Decoration>[] = [];
    if (change.before) out.push(Decoration.widget({ widget: new Struck(change.before), side: -1 }).range(at));
    if (change.after) out.push(Decoration.mark({ class: "cm-sg-ins" }).range(at, at + change.after.length));
    return Decoration.set(out, true);
  };
  return ViewPlugin.fromClass(class {
    decorations: DecorationSet;
    constructor(view: EditorView) { this.decorations = build(view); }
    update(u: ViewUpdate) {
      if (u.docChanged) this.decorations = build(u.view);
    }
  }, { decorations: (v) => v.decorations });
}

const trackedLook = EditorView.theme({
  ".cm-sg-del": { background: "var(--fail-soft)", color: "var(--fail)", textDecoration: "line-through" },
  ".cm-sg-ins": { background: "var(--pass-soft)", color: "var(--pass)" },
});

export function SuggestEditor({ source, select, label, onText }: {
  /* The section's text the suggestion is made on. */
  source: string;
  /* What to select at first (UTF-16 offsets): the highlighted words, or a caret. */
  select: { from: number; to: number };
  label: string;
  onText(text: string): void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const command = useRef<(c: FormatCommand) => void>(() => {});
  const format = useFormatTarget({ apply: (c) => command.current(c) });
  const focused = useRef(format.focused);
  focused.current = format.focused;
  const told = useRef(onText);
  told.current = onText;
  useEffect(() => {
    if (!host.current) return;
    const view = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: source,
        selection: { anchor: Math.min(select.from, source.length), head: Math.min(select.to, source.length) },
        extensions: [
          history(),
          keymap.of([...historyKeymap, ...defaultKeymap]),
          EditorView.lineWrapping,
          EditorView.contentAttributes.of({ "aria-label": label, "aria-multiline": "true" }),
          EditorView.domEventHandlers({ focus: () => { focused.current(); } }),
          EditorView.updateListener.of((u) => { if (u.docChanged) told.current(u.state.doc.toString()); }),
          pageLook,
          markdownLook(true),
          trackedLook,
          tracked(source),
        ],
      }),
    });
    command.current = (c) => {
      const { from, to } = view.state.selection.main;
      const e = formatEdit(view.state.doc.toString(), from, to, c);
      view.dispatch({ changes: { from: e.from, to: e.to, insert: e.insert }, selection: { anchor: e.anchor, head: e.head }, userEvent: "input.format" });
      view.focus();
    };
    view.focus();
    return () => {
      command.current = () => {};
      view.destroy();
    };
    // The editor is opened once on its text: a new source is a new suggestion (the reader keys it).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return <div ref={host} data-testid="doc-suggest-editor" className="min-h-[1.65em]" />;
}
