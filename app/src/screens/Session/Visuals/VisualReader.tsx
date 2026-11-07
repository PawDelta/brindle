/* An open visual, in the chat's place (design/visualizer-build-plan.html §1 a, §2). The terminal
   stays mounted behind it, hidden: it keeps streaming, and its scrollback and draft survive. Back
   or Esc returns; Esc pressed inside the page reaches the host as the page's brindle.esc.

   The page is in a labelled "sandboxed · no network" border, so nothing it draws passes for
   brindle's own. A choice it posts only highlights an option in the answer bar; the answer goes
   from the bar's Send, a real click on host chrome, and only with one of the tool call's own
   option ids. Messages from any other visual, or an older revision, are dropped. */

import { useEffect, useRef, useState, type ComponentType, type ReactNode } from "react";
import { DaemonError, type VisualDTO } from "../../../api/client";
import { plain } from "../../../api/wording";
import { LoadingButton } from "../../../components/LoadingButton";
import { button, field, main, mono } from "../../../components/ui";
import type { VisualsClient } from "./client";
import { setFocus, useFocus } from "./focus";
import { VisualFrame, frameMessage, sameSource, type VisualFrameProps, type VisualSource } from "./frame";

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

/* The reader's frame and chrome, the session's and Knowledge's (read-only) alike. */
export function ReaderChrome({ title, source, missing, meta, actions, onBack, asking, onChoice, footer, focusable = true, Frame = VisualFrame }: {
  title: string;
  /* Focus (and ⌘.) hides the sidebar: only where the sidebar steps aside, a session's reader. */
  focusable?: boolean;
  source: VisualSource | null;
  /* What shows with no source: still loading, or gone (the default). */
  missing?: string;
  /* The line in the border's corner: id, revision, who published it. */
  meta: string;
  actions?: ReactNode;
  onBack(): void;
  /* The agent asked something new while this was open: a sap badge on Back. */
  asking?: boolean;
  onChoice?(option: string): void;
  footer?: ReactNode;
  Frame?: ComponentType<VisualFrameProps>;
}) {
  const focused = useFocus();
  const back = useRef(onBack);
  back.current = onBack;
  const focusedRef = useRef(focused);
  focusedRef.current = focused;
  const focusableRef = useRef(focusable);
  focusableRef.current = focusable;
  useEffect(() => {
    // Esc goes back, unless a field or menu took it first.
    const esc = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      // In the note field, Esc first lets go of it, so a half-typed note isn't lost.
      const typing = e.target instanceof HTMLElement && e.target.closest("input, textarea");
      if (typing) (typing as HTMLElement).blur();
      else back.current();
    };
    // ⌘. (Ctrl+. off a Mac) turns Focus on and off.
    const dot = (e: KeyboardEvent) => {
      if (!focusableRef.current || e.key !== "." || !(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
      e.preventDefault();
      setFocus(!focusedRef.current);
    };
    window.addEventListener("keydown", esc);
    window.addEventListener("keydown", dot);
    return () => {
      window.removeEventListener("keydown", esc);
      window.removeEventListener("keydown", dot);
    };
  }, []);
  const posted = (data: unknown, from: VisualSource) => {
    if (!source || !sameSource(from, source)) return;
    const m = frameMessage(data);
    if (m?.type === "brindle.esc") back.current();
    else if (m?.type === "choice") onChoice?.(m.option);
  };
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col" data-testid="visual-reader">
      <header className="flex flex-wrap items-center gap-s1 border-b border-line px-s2 py-1">
        <button type="button" className={`${button} relative`} onClick={onBack} title="Back (esc)" data-testid="visual-back">
          ← Back <kbd className="font-mono text-[10.5px] text-ink-3">Esc</kbd>
          {asking && <span className="absolute -right-1 -top-1 size-2 rounded-full bg-sap" aria-label="the agent asks something new" data-testid="visual-back-asking" />}
        </button>
        <h2 className="min-w-0 flex-1 truncate text-sm font-semibold">{title}</h2>
        {actions}
        {focusable && (
          <button type="button" className={button} aria-pressed={focused} title="Hide the sidebar (⌘.)" data-testid="visual-focus" onClick={() => setFocus(!focused)}>
            {focused ? "Focus ✓" : "Focus"}
          </button>
        )}
      </header>
      <div className="flex min-h-0 flex-1 flex-col p-s2">
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-line-strong" data-testid="visual-sandbox">
          <div className={`${mono} flex items-center justify-between gap-s2 border-b border-line px-s2 py-0.5 text-[11px] text-ink-3`}>
            <span>sandboxed · no network</span>
            <span className="truncate">{meta}</span>
          </div>
          {source ? <Frame key={JSON.stringify(source)} source={source} title={title} onMessage={posted} /> : <p className="p-s3 text-sm text-ink-3">{missing ?? "This visual isn't here any more."}</p>}
        </div>
      </div>
      {footer}
    </div>
  );
}

export function VisualReader({ client, visual, loading, onBack, asking, onAnswered, onRead, Frame }: {
  /* The engine marked this revision read. */
  onRead?(id: string, rev: number): void;
  client: VisualsClient;
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
    // No bus event says a visual was read: the list hears it from here, for the revision marked.
    if (id && unread) client.markVisualRead(id).then(() => readRef.current?.(id, rev), () => undefined);
  }, [client, id, rev, unread]);
  const [picked, setPicked] = useState<{ key: string; option: string } | null>(null);
  const key = visual ? `${visual.id}:${visual.rev}` : "";
  const options = visual?.options ?? [];
  const choose = (option: string) => {
    if (visual && !visual.answer && options.some((o) => o.id === option)) setPicked({ key, option });
  };
  const [keeping, setKeeping] = useState(false);
  const [keepError, setKeepError] = useState<string | null>(null);
  const keep = async () => {
    if (!visual) return;
    setKeeping(true);
    setKeepError(null);
    try {
      onAnswered(await (visual.kept ? client.unkeepVisual(visual.id) : client.keepVisual(visual.id)));
    } catch (e) {
      setKeepError(message(e));
    } finally {
      setKeeping(false);
    }
  };
  const source: VisualSource | null = visual ? { kind: "session", id: visual.id, rev: visual.rev } : null;
  return (
    <ReaderChrome
      title={visual?.title ?? "Visual"}
      source={source}
      missing={loading ? "Loading…" : undefined}
      meta={visual ? `${visual.id} · r${visual.rev} · ${visual.by}` : ""}
      onBack={onBack}
      asking={asking}
      onChoice={choose}
      Frame={Frame}
      actions={
        visual && (
          <>
            {keepError && <span className="text-xs text-sap">{keepError}</span>}
            <LoadingButton className={button} busy={keeping} disabled={keeping} aria-pressed={visual.kept} title="Keep a copy in Knowledge › Kept visuals" data-testid="visual-keep" onClick={() => void keep()}>
              {visual.kept ? "Kept ✓" : "Keep"}
            </LoadingButton>
          </>
        )
      }
      footer={visual && options.length > 0 && <AnswerBar key={key} client={client} visual={visual} picked={picked?.key === key ? picked.option : null} onPick={choose} onAnswered={onAnswered} />}
    />
  );
}

/* The question, the options as host buttons, a note, and Send. Answered: what was picked. */
function AnswerBar({ client, visual, picked, onPick, onAnswered }: { client: VisualsClient; visual: VisualDTO; picked: string | null; onPick(option: string): void; onAnswered(v: VisualDTO): void }) {
  const [note, setNote] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const options = visual.options ?? [];
  const answered = visual.answer ? options.find((o) => o.id === visual.answer!.option) : null;
  const send = async () => {
    if (!picked) return;
    setSending(true);
    setError(null);
    try {
      // The revision on screen: the bar is keyed by it, so a newer one is a new bar.
      onAnswered(await client.answerVisual(visual.id, visual.rev, picked, note.trim()));
    } catch (e) {
      setError(e instanceof DaemonError && e.status === 409 ? "The agent changed this visual since you opened it. Read the new version and answer again." : `Couldn't send the answer: ${message(e)}`);
    } finally {
      setSending(false);
    }
  };
  return (
    <footer className="flex flex-col gap-s1 border-t border-line px-s2 py-s1" data-testid="visual-answer">
      {visual.question && <p className="text-sm text-ink-2">{visual.question}</p>}
      {visual.answer ? (
        <p className="text-sm" role="status" data-testid="visual-answered">
          You picked <span className="font-semibold">{answered?.label ?? visual.answer.option}</span>
          {visual.answer.note && <span className="text-ink-2"> · {visual.answer.note}</span>}
        </p>
      ) : (
        <div className="flex flex-wrap items-center gap-s1">
          <div role="radiogroup" aria-label="Options" className="flex flex-wrap gap-1">
            {options.map((o) => (
              <button key={o.id} type="button" role="radio" aria-checked={picked === o.id} title={o.detail} className={`${button} ${picked === o.id ? "border-branch bg-branch-soft" : ""}`} onClick={() => onPick(o.id)}>
                {o.label}
              </button>
            ))}
          </div>
          <input className={`${field} min-w-40 flex-1`} aria-label="Add a note" placeholder="Add a note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
          <LoadingButton className={main} busy={sending} disabled={!picked || sending} data-testid="visual-send" onClick={() => void send()}>
            Send answer
          </LoadingButton>
        </div>
      )}
      {error && (
        <p role="alert" className="text-xs text-sap">
          {error}
        </p>
      )}
    </footer>
  );
}
