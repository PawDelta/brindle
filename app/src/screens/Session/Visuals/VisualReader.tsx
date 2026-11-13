/* An open visual takes the whole window (design/visualizer-build-plan-amendments.md item 1): the
   sidebar and the session's bar hide, and the terminal stays mounted behind, hidden: it keeps
   streaming, and its scrollback and draft survive. Back or Esc returns to the session as it was;
   Esc pressed inside the page reaches the host as the page's brindle.esc.

   The page is in a labelled "sandboxed · no network" border, so nothing it draws passes for
   brindle's own. A choice it posts only highlights an option in the answer bar; the answer goes
   from the bar's Send, a real click on host chrome, with an option id or the person's own
   text. Messages from any other visual, or an older revision, are dropped. */

import { useEffect, useLayoutEffect, useRef, useState, type ComponentType, type ReactNode } from "react";
import { DaemonError, type DaemonClient, type VisualDTO } from "../../../api/client";
import { isTauri } from "@tauri-apps/api/core";
import { clipboardAttachments, listenForDrops, type Attachment } from "../../../api/attachments";
import { LoadingButton } from "../../../components/LoadingButton";
import { button, field, main, mono } from "../../../components/ui";
import type { PlanDocClient, PlansClient, VisualsClient } from "./client";
import { BackButton, KeepButton, fileAttachment, fileName, message, useEscBack } from "./chrome";
import { PlanReader } from "./PlanReader";
import { VisualFrame, frameMessage, sameSource, type VisualFrameProps, type VisualSource } from "./frame";
import { ExportButton } from "./ExportButton";
import type { ExportClient } from "../../Knowledge/ExportMenu";

/* The reader's frame and chrome, the session's and Knowledge's (read-only) alike. */
export function ReaderChrome({ title, source, missing, meta, actions, onBack, backLabel = "Back", asking, onChoice, footer, Frame = VisualFrame, exportClient, repo }: {
  exportClient?: ExportClient;
  repo?: string;
  title: string;
  source: VisualSource | null;
  /* What shows with no source: still loading, or gone (the default). */
  missing?: string;
  /* The line in the border's corner: id, revision, who published it. */
  meta: string;
  actions?: ReactNode;
  onBack(): void;
  /* Back's own words, where "Back" alone wouldn't say where to. */
  backLabel?: string;
  /* The agent asked something new while this was open: a sap badge on Back. */
  asking?: boolean;
  onChoice?(option: string): void;
  footer?: ReactNode;
  Frame?: ComponentType<VisualFrameProps>;
}) {
  const [snapshot, setSnapshot] = useState<{ source: VisualSource; html: string } | null>(null);
  const [snapshotRequest, setSnapshotRequest] = useState(0);
  const back = useEscBack(onBack);
  const [exporting, setExporting] = useState(false);
  const posted = (data: unknown, from: VisualSource) => {
    if (!source || !sameSource(from, source)) return;
    const m = frameMessage(data);
    if (m?.type === "brindle.esc") back.current();
    else if (m?.type === "choice") onChoice?.(m.option);
    else if (m?.type === "brindle.snapshot") setSnapshot({ source: from, html: m.html });
  };
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col" data-testid="visual-reader">
      {/* One toolbar: Back (Esc) and the title on the left, the visual's own actions on the right. */}
      <header className="flex flex-wrap items-center gap-x-2 gap-y-1 px-s2 pt-2.5">
        <BackButton onBack={onBack} label={backLabel} asking={asking} />
        <span className="mx-1 h-5 w-px bg-line" aria-hidden />
        <h2 className="min-w-0 flex-1 truncate text-[15px] font-semibold tracking-[-0.01em]">{title}</h2>
        {actions}
        {exportClient && source?.kind === "session" && <ExportButton key={`${source.id}:${source.rev}`} client={exportClient} repo={repo}
          open={exporting} onOpenChange={setExporting}
          source={snapshot && sameSource(snapshot.source, source) ? { kind: "visual", id: source.id, rev: source.rev, html: snapshot.html } : null}
          onRequestSnapshot={() => { setSnapshot(null); setSnapshotRequest((n) => n + 1); }} />}
      </header>
      <div className="flex min-h-0 flex-1 flex-col p-s2">
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-line-strong" data-testid="visual-sandbox">
          <div className={`${mono} flex items-center justify-between gap-s2 border-b border-line px-s2 py-0.5 text-[11px] text-ink-3`}>
            <span>sandboxed · no network</span>
            <span className="truncate">{meta}</span>
          </div>
          {source ? <Frame key={JSON.stringify(source)} source={source} title={title} onMessage={posted} snapshotRequest={snapshotRequest} covered={exporting} /> : <p className="p-s3 text-sm text-ink-3">{missing ?? "This visual isn't here any more."}</p>}
        </div>
      </div>
      {footer}
    </div>
  );
}

export function VisualReader({ client, visual, loading, onBack, asking, onAnswered, onRead, Frame, repo }: {
  repo?: string;
  /* The engine marked this revision read. */
  onRead?(id: string, rev: number): void;
  client: VisualsClient & Partial<ExportClient> & Partial<PlansClient> & Partial<PlanDocClient> & Partial<Pick<DaemonClient, "attach">>;
  /* The visual opened; null when the session has no such visual (gone, or a link to another session's). */
  visual: VisualDTO | null;
  /* The session's visuals aren't read yet. */
  loading?: boolean;
  onBack(): void;
  asking?: boolean;
  /* The engine's row after an answer or a Keep, ahead of the bus. */
  onAnswered(visual: VisualDTO): void;
  Frame?: ComponentType<VisualFrameProps>;
}) {
  const id = visual?.id;
  const rev = visual?.rev ?? 0;
  const unread = !!visual?.unread;
  const readRef = useRef(onRead);
  readRef.current = onRead;
  useEffect(() => {
    // Update the list locally for this revision; the read event refreshes the Inbox too.
    if (id && unread) client.markVisualRead(id, rev).then(() => readRef.current?.(id, rev), () => undefined);
  }, [client, id, rev, unread]);
  const [picked, setPicked] = useState<{ key: string; option: string } | null>(null);
  const key = visual ? `${visual.id}:${visual.rev}` : "";
  const options = visual?.options ?? [];
  const choose = (option: string) => {
    if (visual && !visual.answer && (option === "" || options.some((o) => o.id === option))) {
      setPicked((current) => current?.key === key && current.option === option ? null : { key, option });
    }
  };
  const source: VisualSource | null = visual ? { kind: "session", id: visual.id, rev: visual.rev } : null;
  if (visual?.kind === "plan" && client.plan && client.decidePlan) {
    // A plan is brindle's own page, rendered here from its markdown: no frame.
    return <PlanReader client={client as VisualsClient & PlansClient & PlanDocClient & Partial<ExportClient> & Partial<Pick<DaemonClient, "attach">>} visual={visual} repo={repo} onBack={onBack} asking={asking} onChanged={onAnswered} />;
  }
  return (
    <ReaderChrome
      title={visual?.title ?? "Visual"}
      source={source}
      missing={loading ? "Loading…" : undefined}
      meta={visual ? `${visual.id} · r${visual.rev} · ${visual.by}` : ""}
      onBack={onBack}
      asking={asking}
      onChoice={(option) => { if (options.some((o) => o.id === option)) choose(option); }}
      Frame={Frame}
      repo={repo}
      exportClient={client.exportVisual && client.exportDoc && client.docPolicy && client.docExportConfig ? client as ExportClient : undefined}
      actions={
        visual && <KeepButton client={client} visual={visual} onKept={onAnswered} />
      }
      footer={visual && options.length > 0 && <AnswerBar key={key} client={client} visual={visual} picked={picked?.key === key ? picked.option : null} onPick={choose} onAnswered={(answered) => {
        onAnswered(answered);
        onBack();
      }} />}
    />
  );
}

/* The question, the options as host buttons, a note, and Send. Answered: what was picked. */
function AnswerBar({ client, visual, picked, onPick, onAnswered }: { client: VisualsClient & Partial<Pick<DaemonClient, "attach">>; visual: VisualDTO; picked: string | null; onPick(option: string): void; onAnswered(v: VisualDTO): void }) {
  const [note, setNote] = useState("");
  // Insert text immediately so later edits and pastes keep their normal order.
  // Tag only the inserted characters: a native file result removes its filename,
  // while text-only results and clipboard errors leave the person's edits intact.
  type NoteChar = { char: string; paste?: symbol; offset?: number };
  const noteChars = useRef<NoteChar[]>([]);
  const replacedNotes = useRef(new Map<symbol, NoteChar[]>());
  const pendingPastes = useRef(0);
  const showNote = () => setNote(noteChars.current.map(part => part.char).join(""));
  const editNote = (value: string) => {
    const before = noteChars.current.map(part => part.char).join("");
    let start = 0;
    while (start < before.length && start < value.length && before[start] === value[start]) start++;
    let end = before.length;
    let nextEnd = value.length;
    while (end > start && nextEnd > start && before[end - 1] === value[nextEnd - 1]) { end--; nextEnd--; }
    noteChars.current.splice(start, end - start, ...value.slice(start, nextEnd).split("").map(char => ({ char })));
    showNote();
  };
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const noteInput = useRef<HTMLInputElement>(null);
  const active = useRef(false);
  useLayoutEffect(() => {
    active.current = true;
    return () => {
      // The bar is keyed by visual and revision; leaving or replacing it invalidates its request.
      active.current = false;
    };
  }, []);
  const [attached, setAttached] = useState<string[]>([]);
  const [uploading, setUploading] = useState(0);
  const bar = useRef<HTMLElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const options = visual.options ?? [];
  // Files go to the agent that receives the answer, and only where its provider takes them.
  const target = visual.answer_agent_id;
  const canAttach = !!client.attach && !!target && visual.accepts_files === true && !visual.answer;
  // `read` yields the files: browser Files are sent as bytes, native drops and clipboard files as paths.
  const upload = async (read: () => Promise<Attachment[]>) => {
    if (!canAttach) return;
    setUploading((n) => n + 1);
    setError(null);
    try {
      const files = await read();
      if (!active.current) return;
      for (const file of files) {
        const got = await client.attach!(target!, file);
        // The bar belongs to one visual revision; a result for another is dropped.
        if (!active.current) return;
        setAttached((list) => [...list, ...got.paths]);
      }
    } catch (e) {
      if (active.current) setError(`Couldn't attach the file: ${message(e)}`);
    } finally {
      if (active.current) setUploading((n) => n - 1);
    }
  };
  const uploadLatest = useRef(upload);
  uploadLatest.current = upload;
  useEffect(() => {
    if (!canAttach) return;
    let gone = false;
    let stop: (() => void) | undefined;
    // Native drops reach the window, not the visual's child webview; only those released over this bar count.
    void listenForDrops((event) => {
      if (gone || event.type !== "drop" || !event.paths?.length) return;
      const under = document.elementFromPoint(event.x!, event.y!);
      if (under && bar.current?.contains(under)) void uploadLatest.current(async () => event.paths!.map((path) => ({ path })));
    }).then((unlisten) => { if (gone) unlisten(); else stop = unlisten; }, () => undefined);
    return () => { gone = true; stop?.(); };
  }, [canAttach]);
  // Empty ids cannot be published; this host-only choice carries no option to the engine.
  const ownWords = picked === "";
  const canSend = picked !== null && (!ownWords || !!note.trim()) && uploading === 0;
  const answered = visual.answer ? options.find((o) => o.id === visual.answer!.option) : null;
  const dismiss = async () => {
    if (sending) return;
    setSending(true);
    setError(null);
    try {
      const result = await client.dismissVisual(visual.id, visual.rev);
      if (!active.current) return;
      onAnswered(result);
    } catch (e) {
      if (!active.current) return;
      setError(e instanceof DaemonError && e.status === 409 ? "The agent changed this visual since you opened it. Read the new version first." : `Couldn't dismiss it: ${message(e)}`);
    } finally {
      if (active.current) setSending(false);
    }
  };
  const send = async () => {
    if (!canSend || sending) return;
    setSending(true);
    setError(null);
    try {
      // The revision on screen: the bar is keyed by it, so a newer one is a new bar.
      const result = await client.answerVisual(visual.id, visual.rev, picked || undefined, note.trim(), ...(attached.length ? [attached] : []));
      // A late response must not update or navigate away from the person's current screen.
      if (!active.current) return;
      onAnswered(result);
    } catch (e) {
      if (!active.current) return;
      setError(e instanceof DaemonError && e.status === 409 ? "The agent changed this visual since you opened it. Read the new version and answer again." : `Couldn't send the answer: ${message(e)}`);
    } finally {
      if (active.current) setSending(false);
    }
  };
  return (
    <footer ref={bar} className="flex flex-col gap-s1 border-t border-line px-s2 py-s1" data-testid="visual-answer">
      {visual.question && <p className="text-sm text-ink-2">{visual.question}</p>}
      {visual.answer ? (
        <p className="text-sm" role="status" data-testid="visual-answered">
          {visual.answer.option == null ? "You answered" : <>You picked <span className="font-semibold">{answered?.label ?? visual.answer.option}</span></>}
          {visual.answer.note && <span className="text-ink-2"> · {visual.answer.note}</span>}
          {!!visual.answer.attachments?.length && <span className="text-ink-2"> · attached {visual.answer.attachments.map(fileName).join(", ")}</span>}
        </p>
      ) : (
        <div className="flex flex-wrap items-center gap-s1" onKeyDown={(e) => {
          // Enter submits from the note, the free-text box and a chosen option; Shift+Enter and IME composition are left alone.
          if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) return;
          // An unchecked option's Enter picks it natively (even when another option is chosen); only the chosen one sends.
          if (e.target instanceof HTMLButtonElement && e.target.getAttribute("role") === "radio" && e.target.getAttribute("aria-checked") !== "true") return;
          // Attach keeps its native activation (opening the picker).
          if (e.target instanceof HTMLButtonElement && e.target.hasAttribute("data-attach")) return;
          e.preventDefault();
          void send();
        }}>
          <div role="radiogroup" aria-label="Options" className="flex flex-wrap gap-1">
            {options.map((o) => (
              <button key={o.id} type="button" role="radio" aria-checked={picked === o.id} title={o.detail} className={`${button} ${picked === o.id ? "border-branch bg-branch-soft" : ""}`} onClick={() => onPick(o.id)}>
                {o.label}
              </button>
            ))}
            <button type="button" role="radio" aria-checked={ownWords} className={`${button} ${ownWords ? "border-branch bg-branch-soft" : ""}`} onClick={() => { onPick(""); noteInput.current?.focus(); }}>
              Something else…
            </button>
          </div>
          <input ref={noteInput} type="text" className={`${field} min-w-40 flex-1`} aria-label={ownWords ? "Your answer" : "Add a note"} placeholder={ownWords ? "Your answer" : "Add a note (optional)"} value={note} onChange={(e) => editNote(e.target.value)}
            onPaste={(e) => {
              if (!canAttach) return;
              const files = Array.from(e.clipboardData.files);
              // A text input strips CR/LF; stored characters and DOM offsets must agree.
              const rawText = e.clipboardData.getData?.("text") ?? "";
              const text = rawText.replace(/[\r\n]/g, "");
              if (isTauri()) {
                // Finder also supplies filename text; remove that insertion if native files are found.
                e.preventDefault();
                const input = e.currentTarget;
                const start = input.selectionStart ?? note.length;
                const end = input.selectionEnd ?? start;
                const paste = Symbol();
                replacedNotes.current.set(paste, text ? noteChars.current.splice(start, end - start, ...text.split("").map((char, offset) => ({ char, paste, offset }))) : []);
                showNote();
                input.value = noteChars.current.map(part => part.char).join("");
                input.setSelectionRange(start + text.length, start + text.length);
                pendingPastes.current++;
                void upload(async () => {
                  try {
                    const copied = await clipboardAttachments();
                    if (!active.current) return [];
                    if (copied.length) {
                      const selected = replacedNotes.current.get(paste) ?? [];
                      // An insertion can be split between the live note and later
                      // selections. Its earliest surviving character owns restoration.
                      let owner: NoteChar[] | undefined;
                      let offset = Infinity;
                      for (const chars of [noteChars.current, ...replacedNotes.current.values()]) {
                        for (const part of chars) {
                          if (part.paste === paste && part.offset! < offset) {
                            owner = chars;
                            offset = part.offset!;
                          }
                        }
                      }
                      const restore = (chars: NoteChar[]) => {
                        const first = chars.findIndex(part => part.paste === paste);
                        const kept = chars.filter(part => part.paste !== paste);
                        if (chars === owner) kept.splice(first, 0, ...selected);
                        return kept;
                      };
                      noteChars.current = restore(noteChars.current);
                      // Later pastes may retain this insertion in their replaced selection.
                      // Reconcile those snapshots too, so they cannot resurrect a filename.
                      for (const [other, chars] of replacedNotes.current) {
                        if (other !== paste) replacedNotes.current.set(other, restore(chars));
                      }
                      replacedNotes.current.delete(paste);
                      showNote();
                      return copied;
                    }
                    if (!rawText) return Promise.all(files.map(fileAttachment));
                    return [];
                  } finally {
                    // A committed text paste still owns its replaced selection while
                    // an earlier file lookup can restore characters inside it.
                    pendingPastes.current--;
                    if (!pendingPastes.current) replacedNotes.current.clear();
                  }
                });
              } else if (files.length) {
                e.preventDefault();
                void upload(() => Promise.all(files.map(fileAttachment)));
              }
            }} />
          {canAttach && (
            <>
              <input ref={picker} type="file" multiple hidden data-testid="visual-attach-input" onChange={(e) => { const files = Array.from(e.target.files ?? []); e.target.value = ""; if (files.length) void upload(() => Promise.all(files.map(fileAttachment))); }} />
              <button type="button" data-attach className={button} onClick={() => picker.current?.click()}>Attach</button>
            </>
          )}
          <LoadingButton className={main} busy={sending} disabled={!canSend || sending} data-testid="visual-send" onClick={() => void send()}>
            Send answer
          </LoadingButton>
          <button type="button" className={button} disabled={sending} data-testid="visual-dismiss" onClick={() => void dismiss()}>Dismiss</button>
        </div>
      )}
      {!visual.answer && (attached.length > 0 || uploading > 0) && (
        <ul className="flex flex-wrap gap-1" aria-label="Attached files">
          {attached.map((path, i) => (
            <li key={`${path}:${i}`} className="flex items-center gap-1 rounded border border-line px-1.5 text-xs">
              {fileName(path)}
              <button type="button" aria-label={`Remove ${fileName(path)}`} className="text-ink-3" onClick={() => setAttached((list) => list.filter((_, j) => j !== i))}>×</button>
            </li>
          ))}
          {uploading > 0 && <li className="text-xs text-ink-3" role="status">Uploading…</li>}
        </ul>
      )}
      {error && (
        <p role="alert" className="text-xs text-sap">
          {error}
        </p>
      )}
    </footer>
  );
}
