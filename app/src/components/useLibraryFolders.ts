/* The Library rail's folders: read while the Library is open and again after a change.
   Only the newest read may set the list, for success and for failure alike; closing the
   Library or changing the client makes any read still in flight obsolete. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { DaemonClient, FolderGroup, FolderVisibility } from "../api/client";

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
  useEffect(() => {
    if (active) void load();
    return () => {
      latest.current++;
    };
  }, [active, load]);
  const create = useCallback(
    async (scope: string, name: string, visibility: FolderVisibility, canAdd: CanAdd) => {
      await client!.createLibraryFolder(scope, name, visibility, canAdd);
      await load();
    },
    [client, load],
  );
  const move = useCallback(
    async (doc: string, scope: string, folder: string) => {
      await client!.moveDocToFolder(doc, scope, folder);
      await load();
    },
    [client, load],
  );
  return { folders, create, move };
}
