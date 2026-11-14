/* The Library rail's folders: read while the Library is open and again after a change.
   Only the newest read may set the list, for success and for failure alike; closing the
   Library or changing the client makes any read still in flight obsolete. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { DaemonClient, FolderGroup, FolderVisibility } from "../api/client";
import { libraryChanged } from "../store/libraryReader";

export type CanAdd = "owner" | "members";

export function useLibraryFolders(client: DaemonClient | null | undefined, active: boolean) {
  const [folders, setFolders] = useState<FolderGroup[]>([]);
  const latest = useRef(0);
  const load = useCallback(async () => {
    if (!client) return;
    const mine = ++latest.current;
    try {
      const got = (await client.libraryFolders()).scopes;
      if (mine === latest.current) setFolders(got);
    } catch {
      if (mine === latest.current) setFolders([]);
    }
  }, [client]);
  // The lifecycle: bumped when the Library closes or the client changes, so a create or
  // move started before then doesn't refresh (and overwrite) afterwards.
  const epoch = useRef(0);
  useEffect(() => {
    if (active) void load();
    return () => {
      latest.current++;
      epoch.current++;
    };
  }, [active, load]);
  const create = useCallback(
    async (scope: string, name: string, visibility: FolderVisibility, canAdd: CanAdd, repo?: string) => {
      const started = epoch.current;
      await client!.createLibraryFolder(scope, name, visibility, canAdd, repo);
      libraryChanged();     // the list and board read the folders again too
      if (started === epoch.current) await load();
    },
    [client, load],
  );
  const move = useCallback(
    async (doc: string, scope: string, folder: string) => {
      const started = epoch.current;
      await client!.moveDocToFolder(doc, scope, folder);
      libraryChanged();     // the design's folder changed: its record is read again wherever it is shown
      if (started === epoch.current) await load();
    },
    [client, load],
  );
  return { folders, create, move };
}
