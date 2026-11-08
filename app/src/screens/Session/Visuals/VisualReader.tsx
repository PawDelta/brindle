/* An open visual takes the whole window (design/visualizer-build-plan-amendments.md item 1): the
   sidebar and the session's bar hide, and the terminal stays mounted behind, hidden: it keeps
   streaming, and its scrollback and draft survive. Back or Esc returns to the session as it was;
   Esc pressed inside the page reaches the host as the page's brindle.esc.

   The page is in a labelled "sandboxed · no network" border, so nothing it draws passes for
   brindle's own. A choice it posts only highlights an option in the answer bar; the answer goes
   from the bar's Send, a real click on host chrome, with an option id or the person's own
   text. Messages from any other visual, or an older revision, are dropped. */

import { useEffect, useRef, useState, type ComponentType, type ReactNode } from "react";
import { DaemonError, type VisualDTO } from "../../../api/client";
import { LoadingButton } from "../../../components/LoadingButton";
import { button, field, main, mono } from "../../../components/ui";
import type { PlansClient, VisualsClient } from "./client";
import { BackButton, KeepButton, message, useEscBack } from "./chrome";
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
  client: VisualsClient & Partial<ExportClient> & Partial<PlansClient>;
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
    if (visual && !visual.answer && (option === "" || options.some((o) => o.id === option))) {
      setPicked((current) => current?.key === key && current.option === option ? null : { key, option });
    }
  };
  const source: VisualSource | null = visual ? { kind: "session", id: visual.id, rev: visual.rev } : null;
  if (visual?.kind === "plan" && client.plan && client.decidePlan) {
    // A plan is brindle's own page, rendered here from its markdown: no frame.
    return <PlanReader client={client as VisualsClient & PlansClient & Partial<ExportClient>} visual={visual} repo={repo} onBack={onBack} asking={asking} onChanged={onAnswered} />;
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
function AnswerBar({ client, visual, picked, onPick, onAnswered }: { client: VisualsClient; visual: VisualDTO; picked: string | null; onPick(option: string): void; onAnswered(v: VisualDTO): void }) {
  const [note, setNote] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const noteInput = useRef<HTMLInputElement>(null);
  const options = visual.options ?? [];
  // Empty ids cannot be published; this host-only choice carries no option to the engine.
  const ownWords = picked === "";
  const canSend = picked !== null && (!ownWords || !!note.trim());
  const answered = visual.answer ? options.find((o) => o.id === visual.answer!.option) : null;
  const send = async () => {
    if (!canSend || sending) return;
    setSending(true);
    setError(null);
    try {
      // The revision on screen: the bar is keyed by it, so a newer one is a new bar.
      onAnswered(await client.answerVisual(visual.id, visual.rev, picked || undefined, note.trim()));
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
          {visual.answer.option == null ? "You answered" : <>You picked <span className="font-semibold">{answered?.label ?? visual.answer.option}</span></>}
          {visual.answer.note && <span className="text-ink-2"> · {visual.answer.note}</span>}
        </p>
      ) : (
        <div className="flex flex-wrap items-center gap-s1" onKeyDown={(e) => {
          // Enter submits from the note, the free-text box and a chosen option; Shift+Enter and IME composition are left alone.
          if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) return;
          // An unchecked option's Enter picks it natively (even when another option is chosen); only the chosen one sends.
          if (e.target instanceof HTMLButtonElement && e.target.getAttribute("role") === "radio" && e.target.getAttribute("aria-checked") !== "true") return;
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
          <input ref={noteInput} type="text" className={`${field} min-w-40 flex-1`} aria-label={ownWords ? "Your answer" : "Add a note"} placeholder={ownWords ? "Your answer" : "Add a note (optional)"} value={note} onChange={(e) => setNote(e.target.value)} />
          <LoadingButton className={main} busy={sending} disabled={!canSend || sending} data-testid="visual-send" onClick={() => void send()}>
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
