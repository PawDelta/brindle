import type { DaemonClient } from "../../../api/client";
import type { PtyViewOpener } from "../../../api/ptyView";

const RECORD_WINDOW = 250;

/* Observe the person's submitted input, never the terminal's output or agent deliveries.
   The native and WSL PTYs share this opener. Unknown editor operations (history,
   completion, etc.) are not treated as chat submissions or guessed consent. */
export function withVisualInput(client: Pick<DaemonClient, "visualInput" | "beginVisualInput" | "refreshVisualInput">, agent: string, open: PtyViewOpener, enabled: () => boolean = () => true): PtyViewOpener {
  return (at, size, handlers) => {
    const view = open(at, size, handlers);
    let draft = "", pending = "", cursor = 0;
    let valid = true, paste = false, closed = false;
    let held: { release(): void } | null = null;
    let observer: Promise<number> | null = null;
    let sequence = 0;
    const submitted = (data: string): string[] => {
      pending += data;
      const messages: string[] = [];
      while (pending) {
        if (pending.startsWith("\x1b")) {
          const sequence = /^\x1b\[[0-9;?<>]*[A-Za-z~]/.exec(pending)?.[0];
          if (!sequence && /^\x1b(?:\[[0-9;?<>]*)?$/.test(pending)) break;
          pending = pending.slice(sequence?.length ?? 2);
          switch (sequence) {
            case "\x1b[200~": paste = true; break;
            case "\x1b[201~": paste = false; break;
            case "\x1b[D": cursor = Math.max(0, cursor - 1); break;
            case "\x1b[C": cursor = Math.min(draft.length, cursor + 1); break;
            case "\x1b[H": cursor = 0; break;
            case "\x1b[F": cursor = draft.length; break;
            case "\x1b[3~": draft = draft.slice(0, cursor) + draft.slice(cursor + 1); break;
            default:
              // Terminal protocol replies and pointer/focus reports are not editor changes.
              if (!sequence || !/^\x1b\[(?:[0-9;?]*[cnRu]|[IO]|<[0-9;]+[Mm])$/.test(sequence)) valid = false;
          }
          continue;
        }
        const char = pending[0];
        pending = pending.slice(1);
        if (!paste && (char === "\r" || char === "\n")) {
          // Short answers may answer a chat choice; the engine folds offers
          // separately from publication consent. Only empty/unknown drafts are skipped.
          if (valid && draft.trim()) messages.push(draft);
          draft = ""; cursor = 0; valid = true;
        } else if (!paste && (char === "\x7f" || char === "\b")) {
          if (cursor) { draft = draft.slice(0, cursor - 1) + draft.slice(cursor); cursor--; }
        } else if (!paste && char === "\x03") {
          draft = ""; cursor = 0; valid = true;
        } else if (!paste && char === "\x15") {
          draft = draft.slice(cursor); cursor = 0;
        } else if (!paste && char === "\x0b") {
          draft = draft.slice(0, cursor);
        } else if (!paste && char === "\x01") cursor = 0;
        else if (!paste && char === "\x05") cursor = draft.length;
        else if (char >= " " || (paste && (char === "\n" || char === "\r"))) {
          draft = draft.slice(0, cursor) + char + draft.slice(cursor); cursor++;
          if (draft.length > 32000) { draft = ""; cursor = 0; valid = false; }
        } else valid = false;
      }
      return messages;
    };
    return {
      ...view,
      write(data) {
        if (closed) return;
        const submittedAt = Date.now() / 1000;
        // Later input, including Ctrl-C, always releases an earlier held Enter.
        held?.release();
        if (!enabled()) {
          draft = pending = ""; cursor = 0; valid = true; paste = false;
          view.write(data);
          return;
        }
        const messages = submitted(data).map(text => ({ text, sequence: ++sequence }));
        if (!messages.length) { view.write(data); return; }
        const controller = new AbortController();
        const expiresAt = submittedAt + RECORD_WINDOW / 1000;
        let ended = false;
        let alarm: ReturnType<typeof setTimeout>;
        const release = () => {
          if (ended) return;
          ended = true;
          clearTimeout(alarm);
          controller.abort();
          held = null;
          if (!closed) view.write(data);
        };
        held = { release };
        alarm = setTimeout(release, RECORD_WINDOW);
        let usedObserver: Promise<number> | null = null;
        // Native IPC may ignore AbortSignal. The engine checks both expiry
        // and persisted generation/sequence inside the consent transaction.
        void (async () => {
          usedObserver = observer ??= client.beginVisualInput(agent, expiresAt, controller.signal).then(r => r.generation);
          const registration = await usedObserver;
          if (controller.signal.aborted) return;
          // Keep the original submission time across refresh: delayed dispatch
          // must not relabel old input with a newer direct message's context.
          const { generation } = await client.refreshVisualInput(agent, registration, expiresAt, controller.signal, submittedAt);
          for (const message of messages) {
            if (controller.signal.aborted) break;
            await client.visualInput(agent, message.text, expiresAt, controller.signal,
              { generation, sequence: message.sequence, submitted_at: submittedAt });
          }
        })().catch(() => { if (observer === usedObserver) observer = null; }).finally(release);
      },
      close() { closed = true; held?.release(); view.close(); },
    };
  };
}
