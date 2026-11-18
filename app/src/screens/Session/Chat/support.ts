/** Whether this engine takes the message box's messages: its /v1/meta says
 * `chat` once its chat routes send and queue for real. Until it has answered,
 * and on any engine that doesn't say so, there is no box: a composer that
 * can't deliver is never shown.
 */
import { useEffect, useState } from "react";
import type { DaemonClient } from "../../../api/client";

export function useChatSupport(client: Pick<DaemonClient, "meta">): boolean {
  const [supported, setSupported] = useState<{ client: unknown; chat: boolean } | null>(null);
  useEffect(() => {
    let current = true;
    client.meta().then(
      (meta) => {
        if (current) setSupported({ client, chat: meta.chat === true });
      },
      () => {},
    );
    return () => {
      current = false;
    };
  }, [client]);
  // Another daemon's answer says nothing about this one.
  return supported?.client === client && supported.chat;
}
