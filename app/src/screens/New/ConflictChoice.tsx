import { useEffect, useId, useRef, useState } from "react";
import type { DaemonClient, SessionConflict } from "../../api/client";
import type { SessionDTO } from "../../api/schema";
import { button, card, main, quiet } from "../../components/ui";
import { useStore } from "../../store/useStore";
import { completion, useGoal } from "../Session/GoalPanel";

const actions: Record<string, { label: string; detail: string }> = {
  o: { label: "Open", detail: "Continue the running session with its work intact." },
  n: { label: "New session in its own worktree", detail: "Start alongside it on a separate branch and checkout." },
  a: { label: "Start another here", detail: "Start in this checkout while existing sessions keep running." },
  p: { label: "Pause it and start fresh", detail: "Pause the selected session, then start in this checkout." },
  r: { label: "Resume", detail: "Continue the paused session with its work intact." },
};

function SessionSummary({ session, client, repo }: { session: SessionDTO; client: DaemonClient; repo: string }) {
  const store = useStore();
  const goal = useGoal(client, repo, session.id, store);
  const { passed, total } = completion(goal?.milestones);
  return <span className="flex min-w-0 flex-col gap-1 break-words">
    <span className="font-medium text-ink">{session.name || session.goal || "Untitled session"}</span>
    {session.name && session.goal && <span className="text-ink-2">{session.goal}</span>}
    <span role="status" data-state={session.status} className="text-xs text-ink-3">
      {session.status === "processing" ? "Working" : session.status === "waiting" ? "Waiting for you" : session.status === "idle" ? "Ready to continue" : session.status === "failed" ? "Needs attention" : session.status.replaceAll("_", " ")}
      {total > 0 && <span> · <span role="progressbar" aria-label="Milestones completed" aria-valuemin={0} aria-valuemax={total} aria-valuenow={passed}>{passed} of {total} milestones complete</span></span>}
    </span>
  </span>;
}

export function ConflictChoice({ client, repo, conflict, onChoose, onCancel }: { client: DaemonClient; repo: string; conflict: SessionConflict; onChoose(key: string, session: string): Promise<boolean>; onCancel(): void }) {
  const defaultPick = conflict.sessions[conflict.default]?.id ?? conflict.sessions[0]?.id ?? "";
  const [picked, setPicked] = useState(defaultPick);
  const selected = conflict.sessions.some((session) => session.id === picked) ? picked : defaultPick;
  useEffect(() => { setPicked(selected); }, [selected]);
  const [busy, setBusy] = useState(false);
  const choosing = useRef(false);
  const primary = useRef<HTMLButtonElement>(null);
  const id = useId();
  const safe = conflict.kind === "paused" ? "r" : "o";
  useEffect(() => { primary.current?.focus(); }, []);
  const choose = async (key: string) => {
    if (choosing.current || !selected) return;
    choosing.current = true;
    setBusy(true);
    try { await onChoose(key, selected); }
    finally { choosing.current = false; setBusy(false); }
  };
  return <div role="group" aria-label="Session already running" aria-busy={busy} className={`${card} flex flex-col gap-s2 pt-s2 text-sm`} onKeyDown={(event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      if (!choosing.current) onCancel();
    }
  }}>
    <p className="font-medium">{conflict.kind === "paused" ? "Continue a paused session" : "Already running in this project"}</p>
    {conflict.sessions.length === 1 ? <SessionSummary session={conflict.sessions[0]} client={client} repo={repo} /> : <fieldset disabled={busy} className="flex flex-col gap-s1">
      <legend className="mb-s1 text-xs text-ink-3">Choose a session</legend>
      {conflict.sessions.map((session) => <label key={session.id} className="flex cursor-pointer items-start gap-s1 rounded-md px-s1 py-s1 hover:bg-surface-2">
        <input type="radio" name={`${id}-session`} value={session.id} checked={selected === session.id} onChange={() => setPicked(session.id)} className="mt-1 accent-ink" />
        <SessionSummary session={session} client={client} repo={repo} />
      </label>)}
    </fieldset>}
    <div className="flex flex-col gap-s1">
      {[...conflict.choices].sort((a, b) => Number(b.key === safe) - Number(a.key === safe)).map((choice) => {
        const action = actions[choice.key];
        return <div key={choice.key} className="flex flex-wrap items-center gap-x-s2 gap-y-1">
          <button ref={choice.key === safe ? primary : undefined} type="button" disabled={busy || !selected} className={choice.key === safe ? main : button} aria-describedby={`${id}-${choice.key}`} onClick={() => void choose(choice.key)}>
            {action?.label ?? choice.label}
          </button>
          <p id={`${id}-${choice.key}`} className="text-xs text-ink-2">{action?.detail ?? choice.label}</p>
        </div>;
      })}
    </div>
    <div><button type="button" disabled={busy} className={quiet} onClick={onCancel}>Cancel</button></div>
  </div>;
}
