import type { DaemonClient } from "../../../api/client";
import type { PtyViewOpener } from "../../../api/ptyView";

/* Observe the person's submitted input, never the terminal's output or agent deliveries.
   The native and WSL PTYs share this opener. Unknown editor operations (history,
   completion, etc.) fold offers but cannot authorize publication from a guessed draft. */
export function withVisualInput(client: Pick<DaemonClient, "visualInput">, agent: string, open: PtyViewOpener): PtyViewOpener {
  return (at, size, handlers) => {
    const view = open(at, size, handlers);
    let draft = "", pending = "", cursor = 0;
    let valid = true, paste = false, closed = false;
    let writes = Promise.resolve();
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
          messages.push(valid ? draft : "");
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
        writes = writes.then(async () => {
          if (closed) return;
          for (const text of submitted(data)) {
            // Record consent before forwarding Enter: a fast first tool must see it.
            await client.visualInput(agent, text).catch(() => undefined);
          }
          if (!closed) view.write(data);
        }).catch(() => undefined);
      },
      close() { closed = true; view.close(); },
    };
  };
}
