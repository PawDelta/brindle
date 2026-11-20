/** The session's message box, as approved (chat-ui5, queue option A): one
 * rounded field under the live CLI with a green edge while it has the focus,
 * the paperclip at its foot on the left and Send on the right.
 * Send owner: composer state, submit/error lifecycle, client.chatSend.
 * Queue owner: snapshot queue chips inside the box, remove/send-now actions.
 * Attach owner: file picker, paste/drop chips and preview; full byte payloads.
 * Pane owner: hosts this box below the live CLI and forwards session drops.
 * Keep these regions independent; coordinate shared prop/API changes.
 */
import { useEffect, useImperativeHandle, useLayoutEffect, useRef, useState, useSyncExternalStore, type ClipboardEvent, type KeyboardEvent, type Ref } from "react";
import { fileAttachment } from "../../../api/attachments";
import { DaemonError, type DaemonClient } from "../../../api/client";
import type { ChatMessageDTO, QueuedChatMessageDTO } from "../../../api/schema";
import { ClockIcon, CloseIcon, FileIcon, PaperclipIcon, TextLinesIcon } from "../../../components/icons";
import { button, main } from "../../../components/ui";

export interface ChatBoxProps {
  client: Pick<DaemonClient, "chatSend" | "chatEnqueue" | "chatRemove" | "chatSendNow">;
  agentId: string;
  busy: boolean;
  queue: QueuedChatMessageDTO[];
  /** Pane forwards dropped files here, including screenshots without paths.
   * Consume each batch once, then call onFilesConsumed. */
  droppedFiles?: File[];
  onFilesConsumed?(): void;
  /** Refresh snapshot after a successful mutation. */
  onChanged?(): void | Promise<void>;
  /** Who the message goes to, as the empty box says it ("Message the supervisor"). */
  recipient?: string;
  /** The CLI's short name, for the line that says a busy one queues. */
  provider?: string;
  /** Files are being dragged over the session: the empty box says where they land. */
  dropping?: boolean;
  /** What's typed or pasted in the agent's pane while the box stands in for its hidden prompt comes here. */
  typing?: Ref<ChatBoxTyping>;
}

/** Text typed elsewhere (the agent's pane) put in the box at its caret, the keyboard moved to the box with it:
 * "" only moves the keyboard (a composition or dead key, whose text the box's own field then takes). */
export interface ChatBoxTyping {
  type(text: string): void;
}

/* A paste over either limit becomes a chip; at or under both it is ordinary text. */
export const PASTE_LINES = 30;
export const PASTE_BYTES = 4096;

const utf8 = new TextEncoder();
const byteCount = (text: string) => utf8.encode(text).length;
/* Lines are the line breaks in the text, not where a long line wraps; a last line with no break after it counts too. */
export function lineCount(text: string): number {
  const breaks = text.split("\n").length - 1;
  return breaks + (text === "" || text.endsWith("\n") ? 0 : 1);
}
const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;
const sized = (bytes: number) => (bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${Math.round(bytes / 1024)} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`);
const said = (e: unknown) => (e instanceof Error ? e.message : String(e));

interface Attached {
  id: number;
  name: string;
  data_base64: string;
  bytes: number;
  /* An image's own bytes as a data URL, for its thumbnail (the app's CSP allows data: images). */
  image: string | null;
  /* From the clipboard, where an image has no name worth showing. */
  pasted: boolean;
}
interface Pasted {
  id: number;
  text: string;
  lines: number;
  bytes: number;
}

const chipClass = "inline-flex h-7 max-w-full items-center gap-2 rounded-md border bg-surface-2 pl-2 pr-1 text-[12.5px] text-ink";
const removeClass = "inline-flex size-5 shrink-0 items-center justify-center rounded-sm text-ink-3 hover:bg-line hover:text-ink focus-visible:outline-2 focus-visible:outline-branch";

/* Each agent's unsent draft (text, attached files, pasted-text chips) lives here, not in the component: the
   box can be unmounted mid-draft (the session's mount condition, a snapshot refresh) and comes back as it was.
   It is emptied by a successful send or by the person; ids are shared so a restored chip never clashes. */
/* The store is the one truth, and every mounted box subscribes to it, so a send that ends while another
   instance of the same agent's box is showing updates that one too. `token` is the request out for the
   agent, if any: it outlives the component, so a remounted box can't send the same draft again. */
interface Slot { text: string; files: Attached[]; pastes: Pasted[]; token: number | null }
const EMPTY: Slot = { text: "", files: [], pastes: [], token: null };
const slots = new Map<string, Slot>();
const watchers = new Set<() => void>();
const watch = (tell: () => void) => {
  watchers.add(tell);
  return () => void watchers.delete(tell);
};
const change = (agent: string, update: (now: Slot) => Slot) => {
  slots.set(agent, update(slots.get(agent) ?? EMPTY));
  watchers.forEach((tell) => tell());
};
let nextChipId = 0;
let nextRequest = 0;
export const clearChatDrafts = () => {
  slots.clear();
  watchers.forEach((tell) => tell());
};
type Next<T> = T | ((now: T) => T);
const resolve = <T,>(next: Next<T>, now: T): T => (typeof next === "function" ? (next as (now: T) => T)(now) : next);

export function ChatBox({ client, agentId, busy, queue, droppedFiles, onFilesConsumed, onChanged, recipient = "Claude Code", provider = "Claude", dropping = false, typing }: ChatBoxProps) {
  // -- send owner -------------------------------------------------------------
  // One-line composer grows to 40vh, then scrolls; controls remain visible.
  // Enter submits, Shift+Enter inserts a newline; IME composition never submits.
  // The draft stays on failure; on success only what was sent is cleared, and
  // only if the box is still the same agent's.
  const { text, files, pastes, token: pending } = useSyncExternalStore(watch, () => slots.get(agentId) ?? EMPTY);
  const setText = (next: Next<string>) => change(agentId, (s) => ({ ...s, text: resolve(next, s.text) }));
  const setFiles = (next: Next<Attached[]>) => change(agentId, (s) => ({ ...s, files: resolve(next, s.files) }));
  const setPastes = (next: Next<Pasted[]>) => change(agentId, (s) => ({ ...s, pastes: resolve(next, s.pastes) }));
  const [error, setError] = useState<string | null>(null);
  const [scrolled, setScrolled] = useState(false);
  const [preview, setPreview] = useState<number | null>(null);
  // Queued here and not in the snapshot yet; removed or sent here and still in it.
  const [held, setHeld] = useState<QueuedChatMessageDTO[]>([]);
  const [gone, setGone] = useState<ReadonlySet<string>>(new Set());
  const [acting, setActing] = useState<ReadonlySet<string>>(new Set());

  const editor = useRef<HTMLTextAreaElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const caret = useRef<number | null>(null);
  const shown = useRef({ agentId, mounted: true });
  shown.current.agentId = agentId;
  useEffect(() => {
    const now = shown.current;
    now.mounted = true;
    return () => { now.mounted = false; };
  }, []);
  const still = (agent: string) => shown.current.mounted && shown.current.agentId === agent;

  // Another agent's box: nothing of the last one's draft carries over.
  const [owner, setOwner] = useState(agentId);
  if (owner !== agentId) {
    setOwner(agentId);
    setError(null);
    setPreview(null);
    setHeld([]);
    setGone(new Set());
    setActing(new Set());
  }

  // The field is as tall as its text; the box's 40vh stops it there and the text scrolls inside.
  const fit = () => {
    const el = editor.current;
    if (!el) return;
    el.style.height = "auto";
    if (el.scrollHeight) el.style.height = `${el.scrollHeight}px`;
    setScrolled(el.scrollTop > 0);
  };
  useLayoutEffect(() => {
    fit();
    if (caret.current !== null) {
      editor.current?.setSelectionRange(caret.current, caret.current);
      caret.current = null;
    }
  }, [text]);
  useEffect(() => {
    const el = editor.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    // A narrower window wraps the text again.
    let width = el.clientWidth;
    const watch = new ResizeObserver(() => {
      if (el.clientWidth === width) return;
      width = el.clientWidth;
      fit();
    });
    watch.observe(el);
    return () => watch.disconnect();
  }, []);

  const refresh = async () => {
    try {
      await onChanged?.();
    } catch {
      // The next snapshot brings it.
    }
  };

  const canSend = text.trim().length > 0 || files.length > 0 || pastes.length > 0;
  // One request out per agent (held in the store, so a remounted box respects it), and each releases the
  // lock once, by its own token: a request that ends late (its snapshot refresh still running) can't
  // unlock one sent after it.
  const submit = async () => {
    if ((slots.get(agentId) ?? EMPTY).token !== null || !canSend) return;
    const token = ++nextRequest;
    const release = () => change(started.agentId, (s) => (s.token === token ? { ...s, token: null } : s));
    const started = { agentId, text, files, pastes };
    change(agentId, (s) => ({ ...s, token }));
    // What was typed, then every paste in full, in the order pasted.
    const body = [...(text.trim() ? [text] : []), ...pastes.map((p) => p.text)].join("\n");
    const message: ChatMessageDTO = files.length
      ? { text: body, attachments: files.map(({ name, data_base64 }) => ({ name, data_base64 })) }
      : { text: body };
    setError(null);
    try {
      // A busy agent's message waits in brindle's queue; the engine may still queue one sent to an idle agent.
      const result = await (busy ? client.chatEnqueue(agentId, message) : client.chatSend(agentId, message));
      // What was sent leaves the agent's draft, whichever box (if any) is showing it; later edits stay.
      change(started.agentId, (s) => ({
        text: s.text === started.text ? "" : s.text,
        files: s.files.filter((f) => !started.files.includes(f)),
        pastes: s.pastes.filter((p) => !started.pastes.includes(p)),
        token: s.token === token ? null : s.token,
      }));
      if (!still(started.agentId)) return;
      setPreview(null);
      const id = result.status === "queued" ? result.message_id : null;
      if (id) setHeld((now) => [...now, { id, text: body, attachments: started.files.map((f) => f.name) }]);
      await refresh();
      if (id && still(started.agentId)) setHeld((now) => now.filter((m) => m.id !== id));
    } catch (e) {
      if (still(started.agentId)) setError(`Not sent: ${said(e)}`);
    } finally {
      release();
    }
  };
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing || e.keyCode === 229) return;
    e.preventDefault();
    void submit();
  };

  // -- queue owner ------------------------------------------------------------
  // The snapshot is authoritative. Option A: one line per chip inside the box,
  // above the draft, with Send now and remove on each.
  const waiting = [...queue, ...held.filter((h) => !queue.some((q) => q.id === h.id))].filter((m) => !gone.has(m.id));
  // Messages Send now left queued because the agent waits on a permission
  // request only the person answers: their chip says so, not the error line.
  const [unanswered, setUnanswered] = useState<ReadonlySet<string>>(new Set());
  const actOn = async (id: string, action: "remove" | "now") => {
    if (acting.has(id)) return;
    setUnanswered((now) => {
      if (!now.has(id)) return now;
      const next = new Set(now);
      next.delete(id);
      return next;
    });
    const agent = agentId;
    const mark = (set: (update: (now: ReadonlySet<string>) => ReadonlySet<string>) => void, on: boolean) =>
      set((now) => {
        const next = new Set(now);
        if (on) next.add(id);
        else next.delete(id);
        return next;
      });
    mark(setActing, true);
    // A removed message leaves at once; one sent now stays, dimmed, until the engine has it.
    if (action === "remove") mark(setGone, true);
    setError(null);
    try {
      await (action === "remove" ? client.chatRemove(agent, id) : client.chatSendNow(agent, id));
      if (!still(agent)) return;
      mark(setGone, true);
      await refresh();
    } catch (e) {
      if (!still(agent)) return;
      if (action === "now" && e instanceof DaemonError && e.code === "permission_pending") setUnanswered((now) => new Set(now).add(id));
      else setError(`${action === "remove" ? "Not removed" : "Not sent"}: ${said(e)}`);
    } finally {
      if (still(agent)) {
        mark(setGone, false);
        mark(setActing, false);
      }
    }
  };

  // -- paste/drop owner -------------------------------------------------------
  // Picked, pasted and dropped files are read here as bytes: {name, data_base64},
  // never a path. They wait as chips for Send. A paste over 30 lines or 4096
  // UTF-8 bytes becomes a chip too; typed text never does.
  const addFiles = async (list: File[], pasted = false) => {
    if (!list.length) return;
    const agent = agentId;
    const read = await Promise.allSettled(list.map(fileAttachment));
    if (!still(agent)) return;
    const added = read.flatMap((r, i): Attached[] => {
      if (r.status !== "fulfilled") return [];
      const type = list[i].type;
      return [{ id: ++nextChipId, ...r.value, bytes: list[i].size, image: type.startsWith("image/") ? `data:${type};base64,${r.value.data_base64}` : null, pasted }];
    });
    if (added.length) setFiles((now) => [...now, ...added]);
    const unread = list.filter((_, i) => read[i].status === "rejected").map((f) => f.name);
    setError(unread.length ? `Couldn't read ${unread.join(", ")}.` : null);
  };
  const consumed = useRef<File[] | null>(null);
  useEffect(() => {
    if (!droppedFiles?.length || consumed.current === droppedFiles) return;
    consumed.current = droppedFiles;
    void addFiles(droppedFiles);
    onFilesConsumed?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [droppedFiles]);

  const insert = (pasted: string) => {
    const el = editor.current;
    if (!el) return;
    // The webview's own insertion keeps undo; where there is none, the text is put in at the selection.
    if (typeof document.execCommand === "function" && document.execCommand("insertText", false, pasted)) return;
    const from = el.selectionStart ?? el.value.length;
    const to = el.selectionEnd ?? from;
    caret.current = from + pasted.length;
    setText(el.value.slice(0, from) + pasted + el.value.slice(to));
  };
  useImperativeHandle(typing, () => ({
    type(typed: string) {
      const el = editor.current;
      if (!el) return;
      if (document.activeElement !== el) {
        el.focus({ preventScroll: true });
        el.setSelectionRange(el.value.length, el.value.length);
      }
      if (typed) insert(typed);
    },
  }));
  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const copied = Array.from(e.clipboardData.files ?? []);
    const pasted = e.clipboardData.getData("text/plain").replace(/\r\n?/g, "\n");
    // A copied file comes with its name as text; text with a picture of itself (a spreadsheet's cells) is text.
    const names = new Set(copied.map((f) => f.name));
    const onlyNames = pasted.split("\n").every((line) => !line.trim() || names.has(line.trim()));
    if (copied.length && onlyNames) {
      e.preventDefault();
      void addFiles(copied, true);
      return;
    }
    if (!pasted) return;
    e.preventDefault();
    const lines = lineCount(pasted);
    const bytes = byteCount(pasted);
    if (lines > PASTE_LINES || bytes > PASTE_BYTES) setPastes((now) => [...now, { id: ++nextChipId, text: pasted, lines, bytes }]);
    else insert(pasted);
  };

  const opened = pastes.find((p) => p.id === preview) ?? null;
  const closePreview = () => {
    setPreview(null);
    editor.current?.focus();
  };
  const documents = files.filter((f) => !f.image);
  const pictures = files.filter((f) => f.image);

  return (
    <div
      className="relative shrink-0"
      data-testid="chat-box"
      data-busy={busy || undefined}
      // Esc closes the open preview from wherever the focus is: the draft (where it stays), the chip or the preview.
      onKeyDown={(e) => {
        if (e.key !== "Escape" || preview === null) return;
        e.preventDefault();
        e.stopPropagation();
        closePreview();
      }}
    >
      {opened && <PastePreview paste={opened} onClose={closePreview} />}
      <div
        data-testid="chat-composer"
        style={{ maxHeight: "40vh" }}
        className="flex min-h-0 flex-col rounded-[10px] border border-line-strong bg-field transition-[border-color,box-shadow] duration-150 focus-within:border-pass focus-within:shadow-[0_0_0_3px_var(--pass-soft)]"
      >
        {(waiting.length > 0 || files.length > 0 || pastes.length > 0) && (
          <div className="flex min-h-0 shrink flex-col gap-2 overflow-y-auto px-3 pt-3">
            {waiting.length > 0 && (
              <ul className="flex flex-col gap-1.5" aria-label="Queued messages">
                {waiting.map((m) => (
                  <QueueChip key={m.id} message={m} busy={acting.has(m.id)} unanswered={unanswered.has(m.id) ? provider : null} onSendNow={() => void actOn(m.id, "now")} onRemove={() => void actOn(m.id, "remove")} />
                ))}
              </ul>
            )}
            {documents.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {documents.map((f) => (
                  <FileChip key={f.id} file={f} onRemove={() => setFiles((now) => now.filter((x) => x !== f))} />
                ))}
              </div>
            )}
            {(pictures.length > 0 || pastes.length > 0) && (
              <div className="flex flex-wrap gap-2">
                {pictures.map((f) => (
                  <FileChip key={f.id} file={f} onRemove={() => setFiles((now) => now.filter((x) => x !== f))} />
                ))}
                {pastes.map((p) => (
                  <span key={p.id} data-testid="paste-chip" className={`${chipClass} ${p.id === preview ? "border-pass" : "border-line-strong"}`}>
                    <button
                      type="button"
                      className="inline-flex min-w-0 items-center gap-2 rounded-sm focus-visible:outline-2 focus-visible:outline-branch"
                      aria-label={`Preview pasted text, ${plural(p.lines, "line")}, ${sized(p.bytes)}`}
                      aria-expanded={p.id === preview}
                      onClick={() => {
                        setPreview(p.id === preview ? null : p.id);
                        // The draft keeps the focus (and the box its edge) while the preview is open.
                        editor.current?.focus();
                      }}
                    >
                      <TextLinesIcon size={14} className="shrink-0 text-ink-2" />
                      <span className="text-[13px] font-semibold">Pasted text</span>
                      <span className="truncate font-mono text-ink-3">{plural(p.lines, "line")} · {sized(p.bytes)}</span>
                    </button>
                    <button
                      type="button"
                      className={removeClass}
                      aria-label="Remove pasted text"
                      onClick={() => {
                        setPastes((now) => now.filter((x) => x !== p));
                        if (p.id === preview) setPreview(null);
                      }}
                    >
                      <CloseIcon size={12} />
                    </button>
                  </span>
                ))}
              </div>
            )}
          </div>
        )}
        <textarea
          ref={editor}
          rows={1}
          value={text}
          placeholder={dropping ? "Drop to attach…" : `Message ${recipient}`}
          aria-label={`Message ${recipient}`}
          spellCheck
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          onScroll={(e) => setScrolled(e.currentTarget.scrollTop > 0)}
          style={{
            overflowY: "auto",
            resize: "none",
            minHeight: 22,
            // The top line fades while there is more above it.
            ...(scrolled ? { maskImage: "linear-gradient(to bottom, transparent, #000 26px)", WebkitMaskImage: "linear-gradient(to bottom, transparent, #000 26px)" } : {}),
          }}
          className="mt-3 block w-full shrink bg-transparent px-4 text-sm leading-[22px] text-ink outline-none placeholder:text-ink-3"
        />
        <div className="flex shrink-0 items-center gap-2 px-3 pb-2.5 pt-1.5">
          <button
            type="button"
            className="inline-flex size-7 shrink-0 items-center justify-center rounded-md text-ink-3 hover:bg-surface-2 hover:text-ink focus-visible:outline-2 focus-visible:outline-branch"
            aria-label="Attach files"
            title="Attach files"
            onClick={() => picker.current?.click()}
          >
            <PaperclipIcon size={15} />
          </button>
          <input
            ref={picker}
            type="file"
            multiple
            hidden
            data-testid="chat-file-picker"
            onChange={(e) => {
              void addFiles(Array.from(e.target.files ?? []));
              e.target.value = "";
            }}
          />
          <div className="min-w-0 flex-1 text-[13px]">
            {error ? (
              <p role="alert" className="truncate text-sap" title={error}>{error}</p>
            ) : busy ? (
              <p className="flex items-center gap-2 truncate text-ink-3" data-testid="chat-busy">
                <span className="size-1.5 shrink-0 rounded-full bg-branch" aria-hidden />
                {provider} is working. Send adds your message to the queue.
              </p>
            ) : null}
          </div>
          <button type="button" className={main} disabled={!canSend || pending !== null} onClick={() => void submit().then(() => editor.current?.focus())}>
            Send
          </button>
        </div>
      </div>
    </div>
  );
}

/* One queued message, on one line however long it is: the start of its first line, how many more
   lines there are, Send now and remove. `unanswered`: the CLI's name, when Send now was refused
   because it waits on a permission request the person must answer first. */
function QueueChip({ message, busy, unanswered, onSendNow, onRemove }: { message: QueuedChatMessageDTO; busy: boolean; unanswered: string | null; onSendNow(): void; onRemove(): void }) {
  const lines = lineCount(message.text);
  const first = message.text.split("\n").find((line) => line.trim()) ?? "";
  const attached = message.attachments ?? [];
  return (
    <li data-testid="queue-chip" data-lines={lines} className={`flex h-[30px] items-center gap-2.5 rounded-md border border-line bg-surface-2 pl-2.5 pr-1.5 ${busy ? "opacity-[var(--dim)]" : ""}`}>
      <ClockIcon size={14} className="shrink-0 text-ink-3" />
      <span data-testid="queue-preview" title={message.text} className="min-w-0 flex-1 text-[13.5px] text-ink" style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
        {first || attached.join(", ")}
      </span>
      {lines > 1 && <span className="shrink-0 font-mono text-xs text-ink-3">+{plural(lines - 1, "line")}</span>}
      {attached.length > 0 && !!first && <span className="shrink-0 font-mono text-xs text-ink-3">{plural(attached.length, "file")}</span>}
      {unanswered && (
        <span role="status" data-testid="queue-note" data-state="permission-pending" className="min-w-0 shrink truncate text-xs text-sap">
          Answer {unanswered}'s permission request first
        </span>
      )}
      <button type="button" className={`${button} shrink-0 px-2 text-xs`} disabled={busy} onClick={onSendNow}>
        Send now
      </button>
      <button type="button" className={removeClass} disabled={busy} aria-label="Remove queued message" onClick={onRemove}>
        <CloseIcon size={12} />
      </button>
    </li>
  );
}

/* A file waiting for Send: its name, or for an image its thumbnail, pixel size and weight. */
function FileChip({ file, onRemove }: { file: Attached; onRemove(): void }) {
  const [pixels, setPixels] = useState<string | null>(null);
  return (
    <span data-testid="attachment-chip" className={`${chipClass} border-line-strong ${file.image ? "h-8" : ""}`}>
      {file.image ? (
        <>
          <img src={file.image} alt="" className="h-5 w-8 shrink-0 rounded-sm border border-line object-cover" onLoad={(e) => setPixels(`${e.currentTarget.naturalWidth} × ${e.currentTarget.naturalHeight}`)} />
          <span className="truncate text-[13px] font-semibold">{file.pasted ? "Pasted screenshot" : file.name}</span>
          <span className="shrink-0 font-mono text-ink-3">{pixels ? `${pixels} · ` : ""}{sized(file.bytes)}</span>
        </>
      ) : (
        <>
          <FileIcon size={14} className="shrink-0 text-ink-2" />
          <span className="truncate font-mono">{file.name}</span>
        </>
      )}
      <button type="button" className={removeClass} aria-label={`Remove ${file.image && file.pasted ? "pasted screenshot" : file.name}`} onClick={onRemove}>
        <CloseIcon size={12} />
      </button>
    </span>
  );
}

/* The pasted text, to check it is the right thing: read-only, numbered, scrolling through all of
   it, over the foot of the CLI while it is open. Esc or its close button closes it. */
function PastePreview({ paste, onClose }: { paste: Pasted; onClose(): void }) {
  const numbers = Array.from({ length: Math.max(1, paste.lines) }, (_, i) => i + 1).join("\n");
  return (
    <div
      role="dialog"
      aria-label={`Pasted text, ${plural(paste.lines, "line")}`}
      className="absolute bottom-full left-0 z-20 mb-2 flex max-h-[min(340px,45vh)] w-[min(760px,100%)] flex-col overflow-hidden rounded-[10px] border border-line-strong bg-field shadow-[0_12px_32px_rgba(0,0,0,0.28)]"
    >
      <header className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-2">
        <TextLinesIcon size={14} className="shrink-0 text-ink-2" />
        <span className="text-[13px] font-semibold">Pasted text</span>
        <span className="font-mono text-[12.5px] text-ink-3">{plural(paste.lines, "line")} · {sized(paste.bytes)}</span>
        <span className="flex-1" />
        <button type="button" className={removeClass} aria-label="Close" onClick={onClose}>
          <CloseIcon size={12} />
        </button>
      </header>
      <div className="flex min-h-0 flex-1 overflow-auto py-2 font-mono text-[12.5px] leading-[19px]" tabIndex={0}>
        <pre aria-hidden className="sticky left-0 shrink-0 select-none bg-field pl-3 pr-4 text-right text-ink-3">{numbers}</pre>
        <pre className="pr-4 text-ink">{paste.text}</pre>
      </div>
    </div>
  );
}
