/* The composer's Send ⌘↵: brindle's tinted main button. With sessions
   already in the project, a chevron segment inside the same outline opens
   the other ways to start: Send on a new branch (the default, what Send and
   ⌘↵ do), Pause it and start here, then each session there to open (or,
   paused, to resume) instead, with its status and milestones. ↑ ↓ move
   through the menu, Enter or a click picks, Escape or a click outside
   closes it and focus goes back to the chevron. */

import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import type { DaemonClient } from "../../api/client";
import type { SessionDTO } from "../../api/schema";
import { LoadingButton } from "../../components/LoadingButton";
import { Dot, type DotTone } from "../../components/Sidebar";
import { CheckIcon, ChevronDownIcon } from "../../components/icons";
import { isPaused } from "../../store/overview";
import { useStore } from "../../store/useStore";
import { completion, useGoal } from "../Session/GoalPanel";

/* How a send starts when a session runs here: on a new branch in its own worktree, or pausing it. */
export type SendHow = "n" | "p";

export const sessionLabel = (s: SessionDTO) => s.name || s.goal || "Untitled session";

const said: Record<string, string> = { processing: "Working", waiting: "Waiting for you", idle: "Ready to continue", failed: "Needs attention" };
const tones: Record<string, DotTone> = { processing: "work", waiting: "wait", idle: "idle", failed: "fail" };

export function SendButton({ client, repo, project, sessions, busy, disabled, onSend, onOpen, onResume }: {
  client: DaemonClient;
  repo: string;
  /* The project's name, for the menu's heading. */
  project: string;
  /* The sessions running or paused in the project. */
  sessions: SessionDTO[];
  /* A start under way: Send wears the wait, and nothing more starts. */
  busy: boolean;
  /* Nothing can start yet (the CLIs still being read, one to pick). */
  disabled: boolean;
  onSend(how: SendHow): void;
  onOpen(session: SessionDTO): void;
  onResume(session: SessionDTO): void;
}) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLSpanElement>(null);
  const chevron = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const id = useId();
  const running = sessions.filter((s) => !isPaused(s));
  const others = sessions.length > 0;

  useEffect(() => {
    if (!open) return;
    const outside = (e: MouseEvent) => {
      if (!box.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", outside);
    // The default way, else the first row, takes focus: the arrow keys start there.
    const entries = [...(menu.current?.querySelectorAll<HTMLElement>("[role^=menuitem]:not(:disabled)") ?? [])];
    (entries.find((el) => el.getAttribute("aria-checked") === "true") ?? entries[0])?.focus();
    return () => document.removeEventListener("mousedown", outside);
  }, [open]);
  // Nothing else here any more: no chevron, so no menu either.
  useEffect(() => {
    if (!others) setOpen(false);
  }, [others]);

  const close = () => {
    setOpen(false);
    chevron.current?.focus();
  };
  const keys = (e: KeyboardEvent) => {
    const entries = [...(menu.current?.querySelectorAll<HTMLElement>("[role^=menuitem]:not(:disabled)") ?? [])];
    const at = entries.indexOf(document.activeElement as HTMLElement);
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close();
    } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      entries[(at + step + entries.length) % entries.length]?.focus();
    } else if ((e.key === "Enter" || e.key === " ") && at >= 0) {
      e.preventDefault();
      entries[at].click();
    } else if (e.key === "Tab") {
      setOpen(false);
    }
  };
  const pick = (act: () => void) => () => {
    close();
    act();
  };

  const one = running.length === 1 ? sessionLabel(running[0]) : null;
  const heading =
    running.length === 0
      ? `${sessions.length === 1 ? sessionLabel(sessions[0]) : `${sessions.length} sessions`} paused in ${project}`
      : `${one ?? `${running.length} sessions`} ${one ? "runs" : "run"} in ${project}`;

  return (
    <span ref={box} className="relative inline-flex">
      <span className="inline-flex h-7 rounded-[var(--r)] border border-pass/40 bg-pass/12 text-pass transition-colors hover:border-pass/62">
        <LoadingButton
          type="submit"
          tone="primary"
          wrapClassName="inline-flex"
          busy={busy}
          disabled={busy || disabled}
          aria-keyshortcuts="Meta+Enter"
          className={`inline-flex h-full items-center gap-2 pl-3 pr-2.5 text-[13px] font-medium tracking-[-0.005em] hover:bg-pass/10 focus-visible:relative focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-branch disabled:cursor-not-allowed disabled:opacity-45 ${others ? "rounded-l-[3px]" : "rounded-[3px]"}`}
        >
          Send
          <kbd className="font-sans text-[11px] font-medium tracking-[0.06em] opacity-60" aria-hidden>
            ⌘↵
          </kbd>
        </LoadingButton>
        {others && (
          <button
            ref={chevron}
            type="button"
            className={`grid w-6 place-items-center rounded-r-[3px] border-l border-pass/26 hover:bg-pass/10 focus-visible:relative focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-branch disabled:cursor-not-allowed disabled:opacity-45 ${open ? "bg-pass/20" : ""}`}
            aria-label="Other ways to start"
            aria-haspopup="menu"
            aria-expanded={open}
            aria-controls={open ? id : undefined}
            disabled={busy}
            onClick={() => setOpen((o) => !o)}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown" && !open) {
                e.preventDefault();
                setOpen(true);
              }
            }}
          >
            <ChevronDownIcon size={12} className={`transition-transform ${open ? "rotate-180" : ""}`} />
          </button>
        )}
      </span>
      {open && (
        <div
          ref={menu}
          id={id}
          role="menu"
          aria-label="Other ways to start"
          className="absolute right-0 top-[calc(100%+8px)] z-20 flex w-[360px] flex-col rounded-[var(--r)] border border-line-strong bg-canvas p-1.5 shadow-[0_12px_32px_rgba(0,0,0,.5)]"
          onKeyDown={keys}
        >
          <div className="px-2.5 py-1.5 text-[10.5px] font-semibold uppercase tracking-[0.08em] text-ink-3">{heading}</div>
          <Row role="menuitemradio" checked disabled={disabled} mark={<CheckIcon size={12} />} title="Send on a new branch" detail="Beside it, in its own worktree. The default." end="⌘↵" onClick={pick(() => onSend("n"))} />
          {running.length > 0 && (
            <Row
              role="menuitemradio"
              checked={false}
              disabled={disabled}
              title="Pause it and start here"
              detail={one ? `Pause ${one}, then start in this checkout.` : "Pause the one in this checkout, then start there."}
              onClick={pick(() => onSend("p"))}
            />
          )}
          <div role="separator" className="my-1.5 h-px bg-line" />
          {sessions.map((s) => (
            <SessionRow key={s.id} client={client} repo={repo} session={s} onClick={pick(() => (isPaused(s) ? onResume(s) : onOpen(s)))} />
          ))}
        </div>
      )}
    </span>
  );
}

function Row({ role, checked, disabled, mark, title, detail, end, onClick }: {
  role: "menuitem" | "menuitemradio";
  checked?: boolean;
  disabled?: boolean;
  mark?: ReactNode;
  title: ReactNode;
  detail: ReactNode;
  end?: ReactNode;
  onClick(): void;
}) {
  return (
    <button
      type="button"
      role={role}
      aria-checked={checked}
      disabled={disabled}
      className={`flex w-full items-start gap-2.5 rounded-[var(--r)] px-2.5 py-[7px] text-left text-[13px] text-ink outline-none hover:bg-surface-2 focus-visible:bg-surface-2 disabled:opacity-45 ${checked ? "bg-surface-2" : ""}`}
      onClick={onClick}
    >
      <span className="mt-1 flex w-3 shrink-0 justify-center text-pass" aria-hidden>
        {mark}
      </span>
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="font-medium">{title}</span>
        <span className="text-xs text-ink-2">{detail}</span>
      </span>
      {end && (
        <span className="text-xs text-ink-3" aria-hidden>
          {end}
        </span>
      )}
    </button>
  );
}

/* A session to open (or resume) instead: its status, and how far its milestones are. */
function SessionRow({ client, repo, session, onClick }: { client: DaemonClient; repo: string; session: SessionDTO; onClick(): void }) {
  const store = useStore();
  const goal = useGoal(client, repo, session.id, store);
  const { passed, total } = completion(goal?.milestones);
  const paused = isPaused(session);
  const status = paused ? "Paused" : (said[session.status] ?? session.status.replaceAll("_", " "));
  return (
    <Row
      role="menuitem"
      mark={<Dot tone={paused ? "paused" : (tones[session.status] ?? "idle")} className="mt-0.5" />}
      title={`${paused ? "Resume" : "Open"} ${sessionLabel(session)}`}
      detail={
        <span data-state={paused ? "paused" : session.status}>
          {status}
          {total > 0 && ` · ${passed} of ${total} milestones complete`}
        </span>
      }
      end="→"
      onClick={onClick}
    />
  );
}
