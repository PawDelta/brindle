/* The Inbox's quiet suggestion to tidy up: shown when what brindle could give
   back on disk (store/storage, GET /storage's `reclaimable`) is over 5 GB. It
   removes nothing itself: it goes to Settings › Storage, where every clean is
   asked first. Not now hides it until the Inbox is opened again. Not an item:
   it doesn't count as waiting. */

import { useState } from "react";
import type { DaemonClient } from "../../api/client";
import type { Route } from "../../routes";
import { card } from "../../components/ui";
import { NUDGE_BYTES, bytes, useReclaimable } from "../../store/storage";

const LINK = "text-[11.5px] text-branch hover:underline";

export function StorageNudge({ client, onNavigate }: { client: Pick<DaemonClient, "storage">; onNavigate?(route: Route): void }) {
  const reclaimable = useReclaimable(client);
  const [hidden, setHidden] = useState(false);
  if (hidden || reclaimable === null || reclaimable <= NUDGE_BYTES) return null;
  return (
    <aside className={`${card} flex max-w-[960px] flex-wrap items-baseline gap-x-s2 gap-y-0.5 py-[9px] pl-0.5 text-[12.5px] text-ink-2`} aria-label="Storage" data-testid="storage-nudge">
      <span>brindle could give back {bytes(reclaimable)} of disk space from old worktrees and files.</span>
      {onNavigate && (
        <button type="button" className={LINK} onClick={() => onNavigate({ screen: "settings", section: "storage" })}>
          Review in Settings ›
        </button>
      )}
      <button type="button" className="text-[11.5px] text-ink-3 hover:text-ink" onClick={() => setHidden(true)}>
        Not now
      </button>
    </aside>
  );
}
