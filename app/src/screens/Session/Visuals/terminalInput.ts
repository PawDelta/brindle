import type { DaemonClient } from "../../../api/client";
import type { PtyViewOpener } from "../../../api/ptyView";

const RECORD_WINDOW = 250;

/* Several characters in one write are a paste the terminal didn't bracket: keys arrive one at a time. */
const isBurst = (data: string) => data.length > 4 && !data.startsWith("\x1b");

type Submission = { text: string; typed: boolean; sequence: number };
type Recorder = (messages: Submission[], at: { submittedAt: number; expiresAt: number; signal: AbortSignal }) => Promise<void>;

/* Observe the person's submitted input, never the terminal's output or agent deliveries.
   The native and WSL PTYs share this opener. Unknown editor operations (history,
   completion, etc.) are not treated as chat submissions or guessed consent.
   An Enter that submits something `matters` waits for `recorder` (at most RECORD_WINDOW). */
function observed(open: PtyViewOpener, enabled: () => boolean, recorder: () => Recorder,
                  matters: (message: Submission) => boolean = () => true): PtyViewOpener {
  return (at, size, handlers) => {
    const view = open(at, size, handlers);
    const record = recorder();
    let draft = "", pending = "", cursor = 0;
    let valid = true, paste = false, closed = false, pasted = false;
    let held: { release(): void } | null = null;
    let sequence = 0;
    const submitted = (data: string): { text: string; typed: boolean }[] => {
      pending += data;
      const burst = isBurst(data);
      if (burst) pasted = true;
      const messages: { text: string; typed: boolean }[] = [];
      while (pending) {
        if (pending.startsWith("\x1b")) {
          const sequence = /^\x1b\[[0-9;?<>]*[A-Za-z~]/.exec(pending)?.[0];
          if (!sequence && /^\x1b(?:\[[0-9;?<>]*)?$/.test(pending)) break;
          pending = pending.slice(sequence?.length ?? 2);
          // Home/End can target a line or the whole editor in multiline drafts.
          if (draft.includes("\n") && (sequence === "\x1b[H" || sequence === "\x1b[F")) valid = false;
          switch (sequence) {
            case "\x1b[200~": paste = pasted = true; break;
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
        if (!paste && draft.includes("\n") && /[\x01\x05\x0b\x15]/.test(char)) valid = false;
        if (!paste && char === "\r") {
          // Short answers may answer a chat choice; the engine folds offers
          // separately from publication consent. Only empty/unknown drafts are skipped.
          if (valid && draft.trim()) messages.push({ text: draft, typed: !pasted });
          draft = ""; cursor = 0; valid = true; pasted = burst;
        } else if (!paste && (char === "\x7f" || char === "\b")) {
          if (cursor) { draft = draft.slice(0, cursor - 1) + draft.slice(cursor); cursor--; }
        } else if (!paste && char === "\x03") {
          draft = ""; cursor = 0; valid = true; pasted = burst;
        } else if (!paste && char === "\x15") {
          draft = draft.slice(cursor); cursor = 0;
        } else if (!paste && char === "\x0b") {
          draft = draft.slice(0, cursor);
        } else if (!paste && char === "\x01") cursor = 0;
        else if (!paste && char === "\x05") cursor = draft.length;
        // Ctrl+J inserts a draft newline in Codex; only Enter submits consent.
        else if (char === "\n" || char >= " " || (paste && char === "\r")) {
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
          // Keep following the draft while off, so that turning on halfway through a message
          // can't make the rest of it look like a whole typed message.
          submitted(data);
          view.write(data);
          return;
        }
        const messages = submitted(data).map(message => ({ ...message, sequence: ++sequence })).filter(matters);
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
        void record(messages, { submittedAt, expiresAt, signal: controller.signal }).finally(release);
      },
      close() { closed = true; held?.release(); view.close(); },
    };
  };
}

/* Whether the app should watch the person's typing. Nothing is known until the engine answers /meta, and
   a failed or slow answer must not switch the watching off for good: only an answer of "off" does, and
   then it asks again (at most every RECHECK seconds) so turning visuals on takes effect. The engine
   refuses input it doesn't want either way. */
const RECHECK = 5000;
export function visualsGate(client: { meta(): Promise<{ visuals?: boolean }> } | null | undefined, now: () => number = Date.now) {
  let known: boolean | null = null, asked = -Infinity, closed = false, inFlight = false;
  const ask = () => {
    // One request at a time, so a slow older answer can't overwrite a newer one.
    if (!client || closed || inFlight || now() - asked < RECHECK) return;
    asked = now();
    inFlight = true;
    // A failed re-check leaves it unknown again, not stuck on an older "off".
    client.meta().then(meta => { if (!closed) known = meta.visuals === true; }, () => { known = null; }).finally(() => { inFlight = false; });
  };
  ask();
  return {
    // While an answer is pending, a cached "off" may be out of date: keep recording (the engine decides).
    enabled: () => { if (known !== true) ask(); return known !== false || inFlight; },
    close: () => { closed = true; },
  };
}

export function withVisualInput(client: Pick<DaemonClient, "visualInput" | "beginVisualInput" | "refreshVisualInput">, agent: string, open: PtyViewOpener, enabled: () => boolean = () => true): PtyViewOpener {
  return observed(open, enabled, () => {
    let observer: Promise<number> | null = null;
    return async (messages, { submittedAt, expiresAt, signal }) => {
      let used: Promise<number> | null = null;
      try {
        used = observer ??= client.beginVisualInput(agent, expiresAt, signal).then(r => r.generation);
        const registration = await used;
        if (signal.aborted) return;
        // Keep the original submission time across refresh: delayed dispatch
        // must not relabel old input with a newer direct message's context.
        const { generation } = await client.refreshVisualInput(agent, registration, expiresAt, signal, submittedAt);
        // Native IPC may ignore AbortSignal. The engine checks both expiry
        // and persisted generation/sequence inside the consent transaction.
        for (const message of messages) {
          if (signal.aborted) break;
          await client.visualInput(agent, message.text, expiresAt, signal,
            { generation, sequence: message.sequence, submitted_at: submittedAt, typed: message.typed });
        }
      } catch { if (observer === used) observer = null; }
    };
  });
}

/* Claude and OpenCode report a prompt through their hook, which can't say what was typed rather than
   pasted. Vouch for what the person keyed in, before Enter goes through. Only this makes those words
   quotable: a report that fails or comes late leaves nothing, and the engine refuses a late one. */
export function withTypedWords(client: Pick<DaemonClient, "visualTypedWords">, agent: string, open: PtyViewOpener, enabled: () => boolean = () => true): PtyViewOpener {
  return observed(open, enabled, () => async (messages, { submittedAt, expiresAt, signal }) => {
    try {
      for (const message of messages) {
        if (signal.aborted) break;
        await client.visualTypedWords(agent, message.text, expiresAt, submittedAt, signal);
      }
    } catch { /* not vouched for: the Enter still goes through */ }
  }, message => message.typed);
}
