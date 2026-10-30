/* The repo a per-repo screen shows (Knowledge, Profiles, Settings): the
   first the daemon knows until the person picks another; a picker only when
   there is more than one. */

import { useState } from "react";
import { useSessions } from "../store/sessions";
import { field } from "./ui";

/* `initial`: the repo to start on (a route's own), when it is one the app knows; else the first. */
export function useRepo(initial?: string | null): { repo: string | null; setRepo(repo: string): void; repos: ReturnType<typeof useSessions>["repos"] } {
  const { repos } = useSessions();
  const [picked, setPicked] = useState<string | null>(initial ?? null);
  const repo = picked && repos?.some((r) => r.repo_root === picked) ? picked : (repos?.[0]?.repo_root ?? null);
  return { repo, setRepo: setPicked, repos };
}

export function RepoSelect({ repo, setRepo, repos, disabled = false }: ReturnType<typeof useRepo> & { disabled?: boolean }) {
  if (!repos || repos.length < 2) return null;
  return (
    <select className={field} aria-label="Repo" value={repo ?? ""} disabled={disabled} onChange={(e) => setRepo(e.target.value)}>
      {repos.map((r) => (
        <option key={r.repo_root} value={r.repo_root}>
          {r.name}
        </option>
      ))}
    </select>
  );
}

/* No repo yet: what every per-repo screen says instead. */
export const NO_REPO = "No repo yet: start a session in one and it shows here.";
