/* Earlier CLI sessions are read-only text above the current live terminal.
   The daemon owns their persistent order and marks each restart boundary. */
import { useContext, useEffect, useRef, useState } from "react";
import type { TranscriptMessage } from "../../api/client";
import { DaemonContext } from "../../api/context";

export function RestartHistory({ id, revision }: { id: string; revision: unknown }) {
  const client = useContext(DaemonContext);
  const cursor = useRef("0");
  const [messages, setMessages] = useState<TranscriptMessage[]>([]);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!client) return;
    let current = true;
    // A new snapshot catches a restart even if the pane and status stayed the
    // same. Cursors avoid appending old text again on subsequent snapshots.
    void client.transcript(id, cursor.current).then((data) => {
      if (!current) return;
      cursor.current = data.cursor;
      const earlier = data.messages.filter((m) => m.history);
      setMessages((old) => data.reset ? earlier : [...old, ...earlier]);
      setFailed(false);
    }, () => { if (current) setFailed(true); });
    return () => { current = false; };
  }, [client, id, revision]);

  if (!messages.length && !failed) return null;
  return (
    <details open className="max-h-[40%] shrink-0 overflow-auto border-b border-shell-line bg-canvas px-s3 py-s2" data-testid="restart-history">
      <summary className="cursor-pointer text-xs text-ink-3">Earlier conversation · read only</summary>
      {messages.map((message, i) => message.role === "restart"
        ? <div key={i} role="separator" className="my-s2 border-t border-shell-line pt-s1 text-center text-xs text-ink-3">Supervisor restarted</div>
        : <div key={i} className="my-s2 text-sm" data-role={message.role}>
            <span className="text-xs text-ink-3">{message.role === "user" ? "You" : "Assistant"}</span>
            <p className="whitespace-pre-wrap break-words">{message.text}</p>
          </div>)}
      {failed && <p role="status" className="text-xs text-ink-3">Earlier conversation could not be loaded.</p>}
    </details>
  );
}
