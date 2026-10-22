/* The selected agent's ⋯ menu in the session bar: close (POST
   /agents/{id}/close: stopped if running, its branch and work stay) and
   kill (POST /agents/{id}/kill: stopped and its record deleted), each asked
   about once more before it goes. A worker can be rewound (POST
   /agents/{id}/rewind, a job: its worktree back to a turn, a fresh worker
   there); the supervisor's menu hands the session over (POST
   /sessions/{root}/handover: a new supervisor on another branch or
   worktree, this one paused) and, for a scratch session, moves its work
   into a real repo (POST /sessions/{root}/transfer, a job). Each opens a
   small card under the ⋯ saying what it does before anything goes; the
   provider's session itself is never touched. The one place these live
   now that there is no panel beside the surface. Last, whichever agent is
   shown, the whole session's Close session (POST /sessions/{root}/close):
   no card and nothing asked, its label saying what still runs or isn't
   merged; the view moves on at once.

   The menu stays mounted as the selection changes (it isn't keyed by the
   agent): an open card belongs to the agent it was opened for, and while
   its request or job runs it stays, even when that agent leaves the tree
   (a rewind dismisses the worker before its fresh one exists) and the
   view falls back to the supervisor. Its end is said there either way. */

import { useContext, useEffect, useRef, useState } from "react";
import type { DaemonClient, Rewound, Transferred } from "../../api/client";
import type { AgentDTO, ProfileDTO, WorkspaceDTO } from "../../api/schema";
import { button, card, danger, field, primary, quiet } from "../../components/ui";
import { SessionsContext } from "../../store/sessions";
import { useJob } from "../Branch/useJob";
import { failure, type Notice } from "./notice";
import { repoOfWorkspace } from "./tree";

type Action = "close" | "kill" | "rewind" | "handover" | "transfer";
type Job = ReturnType<typeof useJob>;

/* An open card and the agent (and its workspace) it was opened for. */
interface Open {
  action: Action;
  agent: AgentDTO;
  workspace: WorkspaceDTO;
}

/* Where brindle keeps scratch sessions: <brindle home>/scratch, the home
   being the folder of its database (meta.db_path). */
export const scratchDir = (dbPath: string) => `${dbPath.replace(/\/[^/]*$/, "")}/scratch`;
export const isScratch = (repo: string, dir: string | null) => dir !== null && (repo === dir || repo.startsWith(`${dir}/`));

/* The ⋯'s Close session, saying what is still going on in the session (it
   closes all the same, in one click): "Close session · 2 workers running · 1 unmerged branch". */
export function closeSessionLabel(running: number, unmerged: number): string {
  const parts = ["Close session"];
  if (running > 0) parts.push(`${running} worker${running === 1 ? "" : "s"} running`);
  if (unmerged > 0) parts.push(`${unmerged} unmerged branch${unmerged === 1 ? "" : "es"}`);
  return parts.join(" · ");
}

/* What the session's own ⋯ item needs: its label (closeSessionLabel) and the close itself. */
export interface CloseSessionItem {
  label: string;
  close(): void;
}

/* The session's ⋯ when no agent of it is in the snapshot (a paused or
   failed session's root isn't): Close session alone, one click. */
export function SessionMenu({ closeSession }: { closeSession: CloseSessionItem }) {
  const [menu, setMenu] = useState(false);
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!menu) return;
    const away = (e: MouseEvent) => {
      if (!host.current?.contains(e.target as Node)) setMenu(false);
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [menu]);
  return (
    <div ref={host} className="relative" data-testid="session-menu">
      <button type="button" className={quiet} aria-label="More actions" aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((m) => !m)}>
        ⋯
      </button>
      {menu && (
        <div role="menu" className={`${card} absolute right-0 top-full z-10 mt-1 flex min-w-32 flex-col py-1 shadow-lg`}>
          <button
            type="button"
            role="menuitem"
            className="whitespace-nowrap px-s2 py-1 text-left text-sm hover:bg-surface-2"
            data-testid="close-session"
            onClick={() => (setMenu(false), closeSession.close())}
          >
            {closeSession.label}
          </button>
        </div>
      )}
    </div>
  );
}

export function AgentMenu({ client, agent, workspace, gone = false, root, repo, refresh, onAgent, onSession, closeSession }: {
  client: DaemonClient;
  agent: AgentDTO;
  workspace: WorkspaceDTO;
  /* The agent has left the tree (the last one shown): no ⋯ to open, only what is open still says how it ends. */
  gone?: boolean;
  /* The session's supervisor: its menu hands over; a worker's rewinds. */
  root: string;
  /* The session's repo (a worker's own may be another: see repoOfWorkspace). */
  repo: string;
  refresh: () => Promise<void>;
  /* Shows another agent of the session (the fresh worker a rewind starts). */
  onAgent?(id: string): void;
  /* Opens another session (the one a handover starts). */
  onSession?(root: string, repo: string): void;
  /* The whole session's close, whichever agent is shown: one click, no card. */
  closeSession?: CloseSessionItem;
}) {
  const feed = useContext(SessionsContext);
  const [menu, setMenu] = useState(false);
  const [open, setOpen] = useState<Open | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<Notice>(null);
  const [scratch, setScratch] = useState<string | null>(null);
  const job = useJob();
  const host = useRef<HTMLDivElement>(null);
  // The agent a finished action selected (a rewind's fresh worker): its result stays said once it shows.
  const follow = useRef<string | null>(null);
  const supervisor = agent.id === root;
  const pending = busy || job.busy;

  // Another agent picked: what was open for the last one goes, unless it is still running.
  useEffect(() => {
    if (follow.current === agent.id) {
      follow.current = null;
      return;
    }
    if (pending) return;
    setOpen(null);
    setNote(null);
    setMenu(false);
  }, [agent.id]);

  // The menu closes on a click elsewhere.
  useEffect(() => {
    if (!menu) return;
    const away = (e: MouseEvent) => {
      if (!host.current?.contains(e.target as Node)) setMenu(false);
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [menu]);

  // Whether this session is a scratch one: its repo under brindle's scratch folder.
  useEffect(() => {
    if (!supervisor || scratch !== null) return;
    let live = true;
    client
      .meta()
      .then((m) => live && setScratch(scratchDir(m.db_path)))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [client, supervisor, scratch]);

  const run = async (action: "close" | "kill") => {
    setBusy(true);
    setNote(null);
    try {
      if (action === "close") {
        const got = await client.close(open!.agent.id);
        setNote({ text: got.stopped ? `${got.closed} stopped and closed` : `${got.closed} closed`, failed: false });
      } else {
        const got = await client.kill(open!.agent.id);
        setNote({ text: `${got.killed} killed`, failed: false });
      }
      void refresh();
    } catch (e) {
      setNote(failure(e));
    } finally {
      setBusy(false);
      setOpen(null);
    }
  };

  const repos = feed?.repos ?? [];
  const item = "px-s2 py-1 text-left text-sm hover:bg-surface-2";
  const start = (action: Action) => () => (setMenu(false), setNote(null), job.clear(), setOpen({ action, agent, workspace }));
  const cancel = () => (job.clear(), setOpen(null));
  const done = (text: string) => (setOpen(null), setNote({ text, failed: false }));
  const target = open?.agent;
  return (
    <div ref={host} className="relative" data-testid="agent-menu">
      {!gone && (
        <button type="button" className={quiet} aria-label="More actions" aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((m) => !m)}>
          ⋯
        </button>
      )}
      {menu && !gone && (
        <div role="menu" className={`${card} absolute right-0 top-full z-10 mt-1 flex min-w-32 flex-col py-1 shadow-lg`}>
          {supervisor ? (
            <>
              <button type="button" role="menuitem" className={item} disabled={pending} onClick={start("handover")}>
                Hand over…
              </button>
              {isScratch(repo, scratch) && (
                <button type="button" role="menuitem" className={item} disabled={pending} onClick={start("transfer")}>
                  Move to a repo…
                </button>
              )}
            </>
          ) : (
            <button type="button" role="menuitem" className={item} disabled={pending} onClick={start("rewind")}>
              Rewind…
            </button>
          )}
          <button type="button" role="menuitem" className={item} disabled={pending} onClick={start("close")}>
            Close
          </button>
          <button type="button" role="menuitem" className={`${item} text-sap`} disabled={pending} onClick={start("kill")}>
            Kill
          </button>
          {closeSession && (
            <button
              type="button"
              role="menuitem"
              className={`${item} whitespace-nowrap border-t border-line`}
              data-testid="close-session"
              onClick={() => (setMenu(false), closeSession.close())}
            >
              {closeSession.label}
            </button>
          )}
        </div>
      )}
      {(open || note) && (
        <div className={`${card} absolute right-0 top-full z-10 mt-1 flex w-80 flex-col gap-1 px-s2 py-s1 shadow-lg`} role="dialog" aria-label={open ? `Confirm ${open.action}` : "Result"}>
          {open?.action === "rewind" && (
            <RewindForm
              client={client}
              agent={open.agent}
              repo={repoOfWorkspace(open.workspace, repos, repo)}
              job={job}
              onCancel={cancel}
              onDone={(got, to) => {
                void refresh();
                done(`Rewound to turn ${to}: ${got.agent} starts fresh`);
                follow.current = got.agent;
                onAgent?.(got.agent);
              }}
            />
          )}
          {open?.action === "handover" && (
            <HandoverForm
              client={client}
              root={root}
              busy={busy}
              setBusy={setBusy}
              onCancel={cancel}
              onDone={(id, to) => {
                void refresh();
                done(`Handed over to ${id} on ${to}`);
                onSession?.(id, repo);
              }}
            />
          )}
          {open?.action === "transfer" && (
            <TransferForm
              client={client}
              root={root}
              scratch={repo}
              repos={repos.filter((r) => !isScratch(r.repo_root, scratch))}
              job={job}
              onCancel={cancel}
              onDone={(text) => {
                void refresh();
                void feed?.reload();
                done(text);
              }}
            />
          )}
          {(open?.action === "close" || open?.action === "kill") && (
            <>
              <span className="text-xs text-ink-2">
                {open.action === "close" ? `Close ${target!.id}? It is stopped if running; its branch and work stay.` : `Kill ${target!.id}? It is stopped and its record deleted.`}
              </span>
              <div className="flex items-center gap-1">
                <button type="button" className={danger} disabled={busy} onClick={() => void run(open.action as "close" | "kill")}>
                  {open.action === "close" ? "Confirm close" : "Confirm kill"}
                </button>
                <button type="button" className={button} disabled={busy} onClick={cancel}>
                  Cancel
                </button>
              </div>
            </>
          )}
          {note && !open && (
            <div className="flex items-center gap-s1">
              <span className={`flex-1 text-sm ${note.failed ? "text-sap" : "text-pass"}`}>{note.text}</span>
              <button type="button" className={quiet} onClick={() => setNote(null)}>
                Dismiss
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* The engine's rewind choices: the turn to go back to (its snapshots are
   numbered from 1), the profile the fresh worker runs on (the same unless
   picked; the worker's own repo's profiles, as the engine resolves it),
   and a note for its brief. A turn it has no snapshot of is the job's
   failure, said here. */
function RewindForm({ client, agent, repo, job, onCancel, onDone }: {
  client: DaemonClient;
  agent: AgentDTO;
  repo: string;
  job: Job;
  onCancel(): void;
  onDone(got: Rewound, to: number): void;
}) {
  const [turn, setTurn] = useState("");
  const [profile, setProfile] = useState("");
  const [note, setNote] = useState("");
  const [profiles, setProfiles] = useState<ProfileDTO[]>([]);

  useEffect(() => {
    let live = true;
    client
      .profiles(repo)
      .then((got) => live && setProfiles(got))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [client, repo]);

  const to = /^\d+$/.test(turn.trim()) ? Number(turn.trim()) : 0;
  const rewind = () => {
    if (to < 1 || job.busy) return;
    void job.start(
      `Rewinding ${agent.id}`,
      () => client.rewind(agent.id, { to, ...(profile ? { profile } : {}), ...(note.trim() ? { note: note.trim() } : {}) }),
      (result) => onDone(result as Rewound, to),
    );
  };

  return (
    <>
      <label className="flex items-center gap-s1 text-xs text-ink-2">
        <span className="w-16">Turn</span>
        <input className={`${field} w-20`} inputMode="numeric" aria-label="Turn" value={turn} disabled={job.busy} onChange={(e) => setTurn(e.target.value)} placeholder="3" />
      </label>
      <label className="flex items-center gap-s1 text-xs text-ink-2">
        <span className="w-16">Profile</span>
        <select className={`${field} min-w-0 flex-1`} aria-label="Profile" value={profile} disabled={job.busy} onChange={(e) => setProfile(e.target.value)}>
          <option value="">Same ({agent.profile})</option>
          {profiles
            .filter((p) => p.name !== agent.profile)
            .map((p) => (
              <option key={p.name} value={p.name}>
                {p.name}
              </option>
            ))}
        </select>
      </label>
      <textarea className={`${field} min-h-12 text-xs`} aria-label="Note" value={note} disabled={job.busy} onChange={(e) => setNote(e.target.value)} placeholder="What to do differently (optional)" />
      <span className="text-xs text-ink-2" data-testid="rewind-consequence">
        {agent.id} is stopped and its worktree goes back to how it was after turn {to || "N"}: what it did since, committed or not, is gone. A fresh worker
        starts there with a brief of turns 1–{to || "N"}.
      </span>
      {job.job?.status === "failed" && <span className="text-xs text-sap">{job.job.error}</span>}
      <div className="flex items-center gap-1">
        <button type="button" className={danger} disabled={to < 1 || job.busy} onClick={rewind}>
          {job.busy ? "Rewinding…" : `Rewind to turn ${to || "…"}`}
        </button>
        <button type="button" className={button} disabled={job.busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </>
  );
}

/* The engine's handover: a branch name or a worktree path for the new
   supervisor, and a note it is told first. */
function HandoverForm({ client, root, busy, setBusy, onCancel, onDone }: {
  client: DaemonClient;
  root: string;
  busy: boolean;
  setBusy(busy: boolean): void;
  onCancel(): void;
  onDone(id: string, to: string): void;
}) {
  const [to, setTo] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState<Notice>(null);

  const handover = async () => {
    const target = to.trim();
    if (!target || busy) return;
    setBusy(true);
    setError(null);
    try {
      const got = await client.handover(root, { to: target, ...(note.trim() ? { note: note.trim() } : {}) });
      onDone(got.id, target);
    } catch (e) {
      setError(failure(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <input className={`${field} text-xs`} aria-label="Branch or worktree" value={to} disabled={busy} onChange={(e) => setTo(e.target.value)} placeholder="Branch or worktree path" />
      <textarea className={`${field} min-h-12 text-xs`} aria-label="Handoff note" value={note} disabled={busy} onChange={(e) => setNote(e.target.value)} placeholder="Handoff note: rules, branch state, loose ends (optional)" />
      <span className="text-xs text-ink-2" data-testid="handover-consequence">
        A new supervisor starts on {to.trim() || "that checkout"} with this session's goal, milestones, workers and queued tasks, and the note as its first
        message. {root} is paused.
      </span>
      {error && <span className="text-xs text-sap">{error.text}</span>}
      <div className="flex items-center gap-1">
        <button type="button" className={primary} disabled={!to.trim() || busy} onClick={() => void handover()}>
          {busy ? "Handing over…" : "Confirm handover"}
        </button>
        <button type="button" className={button} disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </>
  );
}

/* scratch.transfer_to_repo: the repo to move into (one brindle knows, or
   any folder inside a git repo) and the branch the work lands on. */
function TransferForm({ client, root, scratch, repos, job, onCancel, onDone }: {
  client: DaemonClient;
  root: string;
  scratch: string;
  repos: { repo_root: string; name: string }[];
  job: Job;
  onCancel(): void;
  onDone(text: string): void;
}) {
  const [pick, setPick] = useState(repos[0]?.repo_root ?? "");
  const [other, setOther] = useState("");
  const [branch, setBranch] = useState("");
  const target = pick || other.trim();
  const named = `brindle/from-${scratch.replace(/\/+$/, "").split("/").at(-1)}`;
  const into = repos.find((r) => r.repo_root === target)?.name ?? target;

  const transfer = () => {
    if (!target || job.busy) return;
    void job.start(
      "Moving the scratch work",
      () => client.transfer(root, { target_path: target, ...(branch.trim() ? { branch: branch.trim() } : {}) }),
      (result) => {
        const got = result as Transferred;
        const name = repos.find((r) => r.repo_root === got.repo_root)?.name ?? got.repo_root.replace(/\/+$/, "").split("/").at(-1);
        onDone(`Moved ${got.commits} commit${got.commits === 1 ? "" : "s"} into ${name} on ${got.branch}`);
      },
    );
  };

  return (
    <>
      <select className={`${field} text-xs`} aria-label="Repository" value={pick} disabled={job.busy} onChange={(e) => setPick(e.target.value)}>
        {repos.map((r) => (
          <option key={r.repo_root} value={r.repo_root}>
            {r.name}
          </option>
        ))}
        <option value="">Another folder…</option>
      </select>
      {!pick && <input className={`${field} text-xs`} aria-label="Repository path" value={other} disabled={job.busy} onChange={(e) => setOther(e.target.value)} placeholder="A folder inside a git repository" />}
      <input className={`${field} text-xs`} aria-label="Branch" value={branch} disabled={job.busy} onChange={(e) => setBranch(e.target.value)} placeholder={named} />
      <span className="text-xs text-ink-2" data-testid="transfer-consequence">
        This scratch session's commits, and any uncommitted work (committed first), are replayed onto a new branch {branch.trim() || named} of{" "}
        {into || "that repository"}, as a workspace there. The scratch session stays, marked as moved, and its chats are paused.
      </span>
      {job.job?.status === "failed" && <span className="text-xs text-sap">{job.job.error}</span>}
      <div className="flex items-center gap-1">
        <button type="button" className={primary} disabled={!target || job.busy} onClick={transfer}>
          {job.busy ? "Moving…" : "Move the work"}
        </button>
        <button type="button" className={button} disabled={job.busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </>
  );
}
