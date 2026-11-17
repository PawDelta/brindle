/* Pick a project, two ways, each one click: open a folder on this Mac (the
   native folder picker, then POST /repos/open), or clone a repository
   (its URL, and the folder to clone into, ~/Projects unless the picker
   says otherwise; POST /repos/clone, a job whose git lines stream into the
   calm progress card, and whose failure, an auth one among them, shows
   git's own words with Retry). The first launch's last step and New
   session both use it. Nothing to type but the URL, no popup.

   Two layouts: "cards" (the first launch's step, the clone form shown), and
   "rows" (New session's first run and its project menu: two equal rows,
   Clone expanding in place into its URL, Into folder and Change…). */

import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { DaemonError, type DaemonClient, type Repo } from "../api/client";
import type { OpenUrl, PickFolder } from "../api/open";
import { plain } from "../api/wording";
import { useJob } from "../screens/Branch/useJob";
import { GithubConnectCard, githubReady, useGithub } from "./GithubConnect";
import { FolderIcon, LinkIcon } from "./icons";
import { ProgressCard } from "./ProgressCard";
import { alert, button, card, field, main, mono, quiet } from "./ui";

export interface ProjectPickerProps {
  client: DaemonClient;
  /* The native folder picker; none in a browser tab, where only a clone works. */
  pickFolder?: PickFolder;
  /* A project is picked (opened, or cloned): its repo. */
  onPicked(repo: Repo): void;
  /* The first launch offers Connect GitHub above clone (skippable); elsewhere it only comes up when a GitHub clone fails. */
  offerGithub?: boolean;
  /* Opens the token page in the system browser. */
  openUrl?: OpenUrl;
  /* "rows": two equal rows, the clone form behind "Clone a repository…". */
  layout?: "cards" | "rows";
  /* Rows only: begin with the folder picker open, or the clone expanded (a menu's Open a folder… or Clone a repository…). */
  start?: "open" | "clone";
  /* Rows only: the clone form opened or closed. */
  onCloning?(open: boolean): void;
}

/* Where a clone goes unless the person picks another folder. */
export const DEFAULT_PARENT = "~/Projects";

const TOOLS_MISSING = "developer_tools_missing";
const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));
const nameOf = (path: string) => path.replace(/\/+$/, "").split("/").at(-1) ?? path;

const GITHUB_HTTPS = /^https:\/\/(www\.)?github\.com\//i;
/* The folder a URL clones into, as git names it. */
const cloneName = (url: string) => nameOf(url.trim().replace(/\.git$/, "")).replace(/^.*:/, "");

export function ProjectPicker({ client, pickFolder, onPicked, offerGithub = false, openUrl, layout = "cards", start: startWith, onCloning }: ProjectPickerProps) {
  const gh = useGithub(client);
  /* The first-launch card was skipped; a clone failure's Connect GitHub was pressed. */
  const [skipped, setSkipped] = useState(false);
  const [failureCard, setFailureCard] = useState(false);
  /* Bumped when the failed clone is dismissed, retried or replaced: a connect that finishes later must not clone again. */
  const generation = useRef(0);
  const [opening, setOpening] = useState(false);
  const [openError, setOpenError] = useState<string | null>(null);
  const [plainFolder, setPlainFolder] = useState<string | null>(null);
  /* git's tools are missing (macOS): the engine's words, command included, and the last folder tried. */
  const [toolsMissing, setToolsMissing] = useState<string | null>(null);
  const [installing, setInstalling] = useState<"idle" | "started" | "failed">("idle");
  const lastOpened = useRef<string | null>(null);
  const [url, setUrl] = useState("");
  const [parent, setParent] = useState(DEFAULT_PARENT);
  const { job, busy, start, clear } = useJob();
  /* The clone's URL as it was pressed (Retry clones that again). */
  const cloning = useRef<{ url: string; parent: string } | null>(null);
  /* Rows: the clone form expanded; the hint's Connect GitHub pressed. */
  const [cloneOpen, setCloneOpen] = useState(startWith === "clone");
  const [askGithub, setAskGithub] = useState(false);
  const toldCloning = useRef(onCloning);
  toldCloning.current = onCloning;
  useEffect(() => {
    if (layout === "rows") toldCloning.current?.(cloneOpen);
  }, [layout, cloneOpen]);

  const open = async (path: string, init = false) => {
    setOpenError(null);
    setPlainFolder(null);
    setToolsMissing(null);
    lastOpened.current = path;
    generation.current++;
    clear();      // an earlier clone's failure is not what Try again retries now
    setOpening(true);
    try {
      onPicked(await client.openRepo(path, init));
    } catch (e) {
      if (e instanceof DaemonError && e.code === TOOLS_MISSING) setToolsMissing(e.message);
      else if (e instanceof DaemonError && e.code === "not_a_repo") setPlainFolder(path);
      else setOpenError(message(e));
    } finally {
      setOpening(false);
    }
  };

  const openFolder = async () => {
    if (!pickFolder) return;
    const path = await pickFolder("Open a folder").catch(() => null);
    if (path) await open(path);
  };
  // Opened from a menu's Open a folder…: the picker comes up at once, once.
  const begun = useRef(false);
  useEffect(() => {
    if (startWith !== "open" || begun.current) return;
    begun.current = true;
    void openFolder();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [startWith]);

  const changeParent = async () => {
    const got = pickFolder ? await pickFolder("Clone into").catch(() => null) : null;
    if (got) setParent(got);
  };

  const clone = (what = cloning.current) => {
    generation.current++;
    if (!what) return;
    cloning.current = what;
    setToolsMissing(null);      // an earlier open's failure is not what Try again retries now
    void start(`Clone ${what.url}`, () => client.cloneRepo(what.url, what.parent));
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (url.trim() && parent.trim()) clone({ url: url.trim(), parent: parent.trim() });
  };

  const missing = toolsMissing ?? (job?.status === "failed" && job.code === TOOLS_MISSING ? job.error ?? "" : null);
  const installTools = async () => {
    try {
      await client.installDeveloperTools();
      setInstalling("started");
    } catch {
      setInstalling("failed");
    }
  };
  const tryAgain = () => {
    setToolsMissing(null);
    setInstalling("idle");
    if (job?.code === TOOLS_MISSING) clone();
    else if (lastOpened.current) void open(lastOpened.current);
  };

  const ready = githubReady(gh.conn);
  /* An open that failed: either layout shows it under Open a folder. */
  const openProblems = (
    <>
      {!pickFolder && <p className="text-xs text-ink-3">The folder picker is the app's; this window has none.</p>}
      {openError && (
        <p className={alert} role="alert">
          {openError}
        </p>
      )}
      {plainFolder && (
        <div className="flex flex-col items-start gap-s1" role="alert">
          <p className="text-sm text-ink-2">{nameOf(plainFolder)} isn't a git repo yet. brindle needs git to give each agent its own branch.</p>
          <div className="flex flex-wrap gap-s1">
            <button type="button" className={button} disabled={opening || busy} onClick={() => void open(plainFolder, true)}>
              Set it up with git
            </button>
            <button type="button" className={button} disabled={opening || busy} onClick={() => void openFolder()}>
              Pick another
            </button>
          </div>
        </div>
      )}
    </>
  );
  /* A GitHub HTTPS clone that failed (not for want of git's tools) while GitHub isn't connected: offer to connect. */
  const githubFailure = job?.status === "failed" && !missing && GITHUB_HTTPS.test(cloning.current?.url ?? "") && gh.conn !== null && !ready;

  // Cloned: the repo is where git put it.
  const result = job?.status === "done" ? (job.result as { path?: string; name?: string } | undefined) : undefined;
  useEffect(() => {
    if (result?.path) onPicked({ repo_root: result.path, name: result.name ?? nameOf(result.path) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result?.path]);

  return (
    <div className="flex flex-col gap-s2" data-testid="project-picker">
      {offerGithub && !skipped && gh.conn !== null && !ready && (
        <GithubConnectCard gh={gh} openUrl={openUrl} onSkip={() => setSkipped(true)} />
      )}
      {offerGithub && ready && (
        <p className="text-sm text-ink-2" data-testid="github-connected">
          GitHub is connected{gh.conn?.login ? ` as @${gh.conn.login}` : ""}.
        </p>
      )}
      {layout === "rows" ? (
        <Rows
          cloneOpen={cloneOpen}
          openButton={
            !plainFolder && (
              <button type="button" className={`${button} ${cloneOpen ? "mt-3" : ""}`} disabled={!pickFolder || opening || busy} onClick={() => void openFolder()}>
                Open a folder…
              </button>
            )
          }
          openProblems={openProblems}
          clone={
            cloneOpen ? (
              <form className="flex min-w-0 flex-1 flex-col" aria-label="Clone a repository" onSubmit={submit}>
                <div className="flex items-baseline justify-between">
                  <span className="font-medium">Clone a repository</span>
                  <button type="button" className={`${quiet} -mr-2`} disabled={busy} onClick={() => setCloneOpen(false)}>
                    Cancel
                  </button>
                </div>
                <div className="mt-2.5 flex items-center gap-2.5">
                  {/* eslint-disable-next-line jsx-a11y/no-autofocus */}
                  <input className={`${field} min-w-0 flex-1 font-mono`} aria-label="Repository URL" placeholder="https://github.com/you/project" autoFocus value={url} disabled={busy} onChange={(e) => setUrl(e.target.value)} />
                  <button type="submit" className={main} disabled={!url.trim() || !parent.trim() || busy}>
                    Clone
                  </button>
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-1 text-xs text-ink-3">
                  Into
                  {pickFolder ? (
                    <>
                      <span className="font-mono text-ink-2" data-testid="clone-parent">
                        {parent.replace(/\/+$/, "")}
                        {cloneName(url) ? `/${cloneName(url)}` : ""}
                      </span>
                      ·
                      <button type="button" className="text-ink-2 underline decoration-line-strong underline-offset-2 hover:text-ink" disabled={busy} onClick={() => void changeParent()}>
                        Change…
                      </button>
                    </>
                  ) : (
                    <input className={`${field} min-w-0 flex-1 font-mono text-xs`} aria-label="Clone into" data-testid="clone-parent" value={parent} disabled={busy} onChange={(e) => setParent(e.target.value)} />
                  )}
                </div>
                {gh.conn !== null && !ready && (
                  <p className="mt-1 text-xs text-ink-3">
                    A private GitHub repo?{" "}
                    <button type="button" className="text-ink-2 underline decoration-line-strong underline-offset-2 hover:text-ink" onClick={() => setAskGithub(true)}>
                      Connect GitHub
                    </button>{" "}
                    first, or clone and we'll ask if it's needed.
                  </p>
                )}
              </form>
            ) : (
              <div className="min-w-0 flex-1">
                <div className="font-medium">A repository URL</div>
                <p className="mb-3 mt-0.5 text-xs text-ink-3">
                  Cloned into <span className="font-mono">{parent}</span>.
                </p>
                <button type="button" className={button} disabled={busy} onClick={() => setCloneOpen(true)}>
                  Clone a repository…
                </button>
              </div>
            )
          }
        />
      ) : (
      <div className="grid gap-s2 sm:grid-cols-2">
        <section className={`${card} flex flex-col gap-s1 p-s2`} aria-label="Open a folder">
          <p className="font-semibold">Open a folder</p>
          <p className="text-sm text-ink-2">A project already on this Mac: brindle works in it where it is.</p>
          {!plainFolder && (
            <div className="mt-auto pt-s1">
              <button type="button" className={button} disabled={!pickFolder || opening || busy} onClick={() => void openFolder()}>
                Open a folder…
              </button>
            </div>
          )}
          {openProblems}
        </section>
        <form className={`${card} flex flex-col gap-s1 p-s2`} aria-label="Clone a repository" onSubmit={submit}>
          <p className="font-semibold">Clone a repository</p>
          <input className={`${field} font-mono`} aria-label="Repository URL" placeholder="https://github.com/you/project.git" value={url} disabled={busy} onChange={(e) => setUrl(e.target.value)} />
          <div className="flex flex-wrap items-center gap-s1 text-xs text-ink-3">
            <label className="flex min-w-0 flex-1 items-center gap-s1">
              Into
              <input className={`${field} min-w-0 flex-1 font-mono`} aria-label="Clone into" data-testid="clone-parent" value={parent} disabled={busy} onChange={(e) => setParent(e.target.value)} />
            </label>
            {pickFolder && (
              <button type="button" className={`${button} px-s1 py-0.5 text-xs`} disabled={busy} onClick={() => void changeParent()}>
                Change…
              </button>
            )}
          </div>
          <p className="text-xs text-ink-3">Clone creates this folder if it doesn't exist yet.</p>
          <div className="mt-auto pt-s1">
            <button type="submit" className={button} disabled={!url.trim() || !parent.trim() || busy}>
              Clone
            </button>
          </div>
        </form>
      </div>
      )}
      {askGithub && !ready && (
        <GithubConnectCard gh={gh} openUrl={openUrl} skipLabel="Not now" onSkip={() => setAskGithub(false)} onConnected={() => setAskGithub(false)} />
      )}
      {missing !== null && (
        <div className={`${card} flex flex-col items-start gap-s1 p-s2`} role="alert" data-testid="developer-tools-missing">
          <p className="font-semibold">brindle needs git's tools first.</p>
          <p className="text-sm text-ink-2">{missing}</p>
          {installing === "started" && <p className="text-sm text-ink-2">Apple's installer is open. When it finishes, try again.</p>}
          {installing === "failed" && <p className="text-sm text-ink-2">The installer didn't start. Run the command above in Terminal.</p>}
          <div className="flex flex-wrap gap-s1">
            <button type="button" className={button} disabled={installing === "started"} onClick={() => void installTools()}>
              Install Command Line Tools
            </button>
            <button type="button" className={button} disabled={opening || busy} onClick={tryAgain}>
              Try again
            </button>
          </div>
        </div>
      )}
      {job && !(job.status === "failed" && job.code === TOOLS_MISSING) && (
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
          other={{
            label: "Dismiss",
            onClick: () => {
              generation.current++;
              clear();
            },
          }}
          extra={
            githubFailure && (
              <button type="button" className={button} onClick={() => setFailureCard(true)}>
                Connect GitHub
              </button>
            )
          }
        />
      )}
      {githubFailure && failureCard && (
        <GithubConnectCard
          gh={gh}
          openUrl={openUrl}
          skipLabel="Not now"
          onSkip={() => setFailureCard(false)}
          onConnected={(() => {
            const at = generation.current;
            return () => {
              // Dismissed, retried or replaced by another operation while connecting: no clone of the old failure.
              if (generation.current !== at) return;
              setFailureCard(false);
              clone();
            };
          })()}
        />
      )}
    </div>
  );
}

/* The rows layout: Open a folder and Clone side by side, equal; the clone
   row widening into its form while the open row narrows to its button. */
function Rows({ cloneOpen, openButton, openProblems, clone }: { cloneOpen: boolean; openButton: ReactNode; openProblems: ReactNode; clone: ReactNode }) {
  return (
    <div className={`grid gap-s2 ${cloneOpen ? "grid-cols-[200px_minmax(0,1fr)]" : "grid-cols-2"}`} data-testid="project-rows" data-state={cloneOpen ? "cloning" : "closed"}>
      <section className="flex items-start gap-3 border-t border-line p-s2" aria-label="Open a folder">
        <FolderIcon size={20} className="mt-0.5 shrink-0 text-ink-2" />
        <div className="flex min-w-0 flex-1 flex-col items-start">
          <div className="font-medium">A folder on this Mac</div>
          {!cloneOpen && <p className="mb-3 mt-0.5 text-xs text-ink-3">brindle works in it where it is.</p>}
          {openButton}
          <div className="mt-s1 flex flex-col items-start gap-s1 empty:hidden">{openProblems}</div>
        </div>
      </section>
      <section className={`flex items-start gap-3 border-t p-s2 ${cloneOpen ? "border-branch bg-branch/5" : "border-line"}`} aria-label={cloneOpen ? undefined : "A repository URL"}>
        <LinkIcon size={20} className={`mt-0.5 shrink-0 ${cloneOpen ? "text-branch" : "text-ink-2"}`} />
        {clone}
      </section>
    </div>
  );
}
