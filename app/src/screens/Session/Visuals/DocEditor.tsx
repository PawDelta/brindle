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
   itself (two would each insert it). When the channel closes the text stays on screen, read only. */

import { Annotation, Compartment, EditorState, type Extension } from "@codemirror/state";
import { EditorView, ViewPlugin, keymap } from "@codemirror/view";
import { defaultKeymap } from "@codemirror/commands";
import { EphemeralStore, LoroDoc, UndoManager } from "loro-crdt";
import { LoroEphemeralPlugin, getCursorEphemeralKey, getUserEphemeralKey, type EphemeralState, type UserState } from "loro-codemirror";
import { useEffect, useRef, useState } from "react";
import type { DaemonClient } from "../../../api/client";

export type LiveClient = Pick<DaemonClient, "visualLive">;

/* How long a writer's caret and name last on others' screens unless written again (ms). */
const PRESENCE_TTL = 30_000;

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
const PEER_COLOURS = 9;
const peerClass = (name: string) => `loro-peer-${[...name].reduce((h, c) => (h * 31 + c.codePointAt(0)!) >>> 0, 7) % PEER_COLOURS}`;
const peerTheme = EditorView.theme(
  Object.fromEntries(
    Array.from({ length: PEER_COLOURS }, (_, i) => [
      [`.loro-cursor.loro-peer-${i}, .loro-cursor.loro-peer-${i}::before`, { backgroundColor: `var(--group-${i + 2})`, color: "var(--color-surface, #fff)" }],
      [`.loro-selection.loro-peer-${i}`, { backgroundColor: `var(--group-${i + 2})` }],
    ]).flat(),
  ),
);
const look = EditorView.theme({
  "&": { fontSize: "12.5px" },
  ".cm-content": { fontFamily: "var(--font-mono, ui-monospace, monospace)", lineHeight: "1.6", padding: "8px 0" },
  "&.cm-focused": { outline: "none" },
});

export function DocEditor({ client, visual, section, name, label, isAuthor = false }: {
  client: LiveClient;
  /* The shared doc's visual id, and the section edited (its id, "lede" for the overview). */
  visual: string;
  section: string;
  /* What teammates see beside this person's caret. */
  name: string;
  /* The editor's accessible name, e.g. "Text of Goals". */
  label: string;
  isAuthor?: boolean;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<"connecting" | "live" | "closed">("connecting");
  // Who is writing is fixed for the editor's life: a new name would be a new caret.
  const named = useRef(name);
  useEffect(() => {
    const doc = new LoroDoc();
    const presence = new EphemeralStore<EphemeralState>(PRESENCE_TTL);
    const undo = new UndoManager(doc, { mergeInterval: 500 });
    const editable = new Compartment();
    const user: UserState = { name: named.current, colorClassName: peerClass(named.current) };
    let view: EditorView | null = null;
    let gone = false;
    let stopRefresh = () => {};
    let locked = true;
    const editability = () => [EditorState.readOnly.of(locked), EditorView.editable.of(!locked)];
    const mount = () => {
      if (!host.current) return null;
      const run = (step: () => boolean) => () => locked || step() || true;
      return new EditorView({
        parent: host.current,
        state: EditorState.create({
          doc: doc.getText(section).toString(),
          extensions: [
            loroSync(doc, section),
            EditorState.transactionFilter.of((tr) => locked && tr.docChanged && !tr.annotation(fromLoro) ? [] : tr),
            LoroEphemeralPlugin(doc, presence, user, (d) => d.getText(section)),
            keymap.of([
              { key: "Mod-z", run: run(() => undo.undo()), preventDefault: true },
              { key: "Mod-Shift-z", run: run(() => undo.redo()), preventDefault: true },
              { key: "Mod-y", run: run(() => undo.redo()), preventDefault: true },
              ...defaultKeymap,
            ]),
            EditorView.lineWrapping,
            EditorView.contentAttributes.of({ "aria-label": label, "aria-multiline": "true" }),
            editable.of(editability()),
            look,
            peerTheme,
          ],
        }),
      });
    };
    const channel = client.visualLive(visual, {
      onState(state) {
        locked = state.lock === "sign_off" || (state.lock === "author_offline" && !isAuthor);
        view?.dispatch({ effects: editable.reconfigure(editability()) });
      },
      onUpdate(bytes) {
        doc.import(bytes);
        if (view || gone) return;
        view = mount();
        setState("live");
      },
      onAwareness(at, bytes) {
        if (at === section) presence.apply(bytes);
      },
      onClose() {
        if (gone) return;
        stopRefresh();
        locked = true;
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
  }, [client, visual, section, isAuthor]);
  return (
    <div className="grid gap-1" data-live={state}>
      <div ref={host} data-testid={`doc-editor-${section}`} className="min-h-24 rounded-md border border-line-strong bg-surface px-2.5" />
      {state !== "live" && (
        <p className="text-xs text-ink-3" role="status">
          {state === "connecting" ? "Connecting to the live text…" : "Disconnected: the text is read only until the doc is opened again."}
        </p>
      )}
    </div>
  );
}
