/* Pick a project, two ways, each one click: open a folder on this Mac (the
   native folder picker, then POST /repos/open), or clone a repository
   (its URL, and the folder to clone into, ~/Projects unless the picker
   says otherwise; POST /repos/clone, a job whose git lines stream into the
   calm progress card, and whose failure, an auth one among them, shows
   git's own words with Retry). The first launch's last step and New
   session both use it. Nothing to type but the URL, no popup. */

import { useEffect, useRef, useState, type FormEvent } from "react";
import type { DaemonClient, Repo } from "../api/client";
import type { PickFolder } from "../api/open";
import { plain } from "../api/wording";
import { useJob } from "../screens/Branch/useJob";
import { ProgressCard } from "./ProgressCard";
import { alert, button, card, field, mono, primary } from "./ui";

export interface ProjectPickerProps {
  client: DaemonClient;
  /* The native folder picker; none in a browser tab, where only a clone works. */
  pickFolder?: PickFolder;
  /* A project is picked (opened, or cloned): its repo. */
  onPicked(repo: Repo): void;
}

/* Where a clone goes unless the person picks another folder. */
export const DEFAULT_PARENT = "~/Projects";

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));
const nameOf = (path: string) => path.replace(/\/+$/, "").split("/").at(-1) ?? path;

export function ProjectPicker({ client, pickFolder, onPicked }: ProjectPickerProps) {
  const [opening, setOpening] = useState(false);
  const [openError, setOpenError] = useState<string | null>(null);
  const [url, setUrl] = useState("");
  const [parent, setParent] = useState(DEFAULT_PARENT);
  const { job, busy, start, clear } = useJob();
  /* The clone's URL as it was pressed (Retry clones that again). */
  const cloning = useRef<{ url: string; parent: string } | null>(null);

  const openFolder = async () => {
    if (!pickFolder) return;
    setOpenError(null);
    const path = await pickFolder("Open a folder").catch(() => null);
    if (!path) return;
    setOpening(true);
    try {
      onPicked(await client.openRepo(path));
    } catch (e) {
      setOpenError(message(e));
      setOpening(false);
    }
  };

  const changeParent = async () => {
    const got = pickFolder ? await pickFolder("Clone into").catch(() => null) : null;
    if (got) setParent(got);
  };

  const clone = (what = cloning.current) => {
    if (!what) return;
    cloning.current = what;
    void start(`Clone ${what.url}`, () => client.cloneRepo(what.url, what.parent));
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (url.trim()) clone({ url: url.trim(), parent });
  };

  // Cloned: the repo is where git put it.
  const result = job?.status === "done" ? (job.result as { path?: string; name?: string } | undefined) : undefined;
  useEffect(() => {
    if (result?.path) onPicked({ repo_root: result.path, name: result.name ?? nameOf(result.path) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result?.path]);

  return (
    <div className="flex flex-col gap-s2" data-testid="project-picker">
      <div className="grid gap-s2 sm:grid-cols-2">
        <section className={`${card} flex flex-col gap-s1 p-s2`} aria-label="Open a folder">
          <p className="font-semibold">Open a folder</p>
          <p className="text-sm text-ink-2">A project already on this Mac: brindle works in it where it is.</p>
          <div className="mt-auto pt-s1">
            <button type="button" className={primary} disabled={!pickFolder || opening || busy} onClick={() => void openFolder()}>
              Open a folder…
            </button>
          </div>
          {!pickFolder && <p className="text-xs text-ink-3">The folder picker is the app's; this window has none.</p>}
          {openError && (
            <p className={alert} role="alert">
              {openError}
            </p>
          )}
        </section>
        <form className={`${card} flex flex-col gap-s1 p-s2`} aria-label="Clone a repository" onSubmit={submit}>
          <p className="font-semibold">Clone a repository</p>
          <input className={`${field} font-mono`} aria-label="Repository URL" placeholder="https://github.com/you/project.git" value={url} disabled={busy} onChange={(e) => setUrl(e.target.value)} />
          <p className="flex flex-wrap items-center gap-s1 text-xs text-ink-3">
            Into <span className={`${mono} text-ink-2`} data-testid="clone-parent">{parent}</span>
            {pickFolder && (
              <button type="button" className={`${button} px-s1 py-0.5 text-xs`} disabled={busy} onClick={() => void changeParent()}>
                Change…
              </button>
            )}
          </p>
          <div className="mt-auto pt-s1">
            <button type="submit" className={primary} disabled={!url.trim() || busy}>
              Clone
            </button>
          </div>
        </form>
      </div>
      {job && (
        <ProgressCard
          label={`Cloning ${cloning.current?.url ?? ""}`}
          testId="clone"
          title={
            <>
              <span className="font-semibold">Cloning</span>
              <span className={`${mono} min-w-0 truncate text-ink-3`}>{cloning.current?.url}</span>
            </>
          }
          status={job.status === "starting" ? "running" : job.status}
          lines={job.progress}
          failure={{ headline: "Clone failed.", message: job.error ?? "" }}
          onRetry={() => clone()}
          other={{ label: "Dismiss", onClick: clear }}
        />
      )}
    </div>
  );
}
