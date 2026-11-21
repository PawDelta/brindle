/* The session header's way in: "Docs" with an icon. It opens the session's list: a plan that holds
   queued tasks pinned on top, then the open ones, newest first, and the decided ones folded under
   "Decided · N". Picking one opens it in the chat's place. The dot stays while a plan holds work,
   and otherwise shows an unread doc. No docs (or an engine without them): no button. */

import { useEffect, useRef, useState } from "react";
import type { VisualDTO } from "../../../api/client";
import { FileIcon } from "../../../components/icons";
import { button, panel } from "../../../components/ui";
import { formatAge } from "../../../store/overview";
import { decided } from "./useVisuals";

export function VisualsButton({ visuals, onOpen }: { visuals: VisualDTO[] | null; onOpen(id: string): void }) {
  const [open, setOpen] = useState(false);
  const [foldOpen, setFoldOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (!box.current?.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      setOpen(false);
    };
    window.addEventListener("mousedown", away);
    window.addEventListener("keydown", esc);
    return () => {
      window.removeEventListener("mousedown", away);
      window.removeEventListener("keydown", esc);
    };
  }, [open]);
  if (!visuals || visuals.length === 0) return null;
  const holding = visuals.find((v) => v.kind === "plan" && (v.holding ?? 0) > 0) ?? null;
  const pending = visuals.filter((v) => !decided(v) && v !== holding);
  const done = visuals.filter(decided);
  const pick = (id: string) => {
    setOpen(false);
    onOpen(id);
  };
  return (
    <div ref={box} className="relative">
      <button type="button" className={`${button} relative px-2 py-1 text-ink-2 hover:bg-sap/10 hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sap`} aria-label="Documents" title="Documents" aria-expanded={open} aria-haspopup="true" data-testid="visuals-button" onClick={() => setOpen((o) => !o)}>
        <FileIcon size={16} />
        {(holding || visuals.some((v) => v.unread)) && (
          <span data-testid="visuals-dot" className="absolute -right-1 -top-1 size-2.5 rounded-full bg-sap ring-2 ring-canvas" aria-label={holding ? "holding" : "unread"} />
        )}
      </button>
      {open && (
        <div className={`${panel} absolute right-0 top-full z-20 mt-1 flex w-80 flex-col gap-px p-1`} role="group" aria-label="Docs" data-testid="visuals-panel">
          {holding && <Holding v={holding} onPick={pick} />}
          {pending.length === 0 && !holding && <p className="px-s1 py-1 text-xs text-ink-3">Nothing waits on you here.</p>}
          {pending.map((v) => (
            <Row key={v.id} v={v} onPick={pick} />
          ))}
          {done.length > 0 && (
            <>
              <button type="button" className="flex items-center gap-1 rounded-md px-s1 py-1 text-left text-[11.5px] text-ink-3 hover:bg-surface-2 hover:text-ink" aria-expanded={foldOpen} data-testid="visuals-decided" onClick={() => setFoldOpen((f) => !f)}>
                <span aria-hidden>{foldOpen ? "▾" : "▸"}</span>
                Decided · {done.length}
              </button>
              {foldOpen && done.map((v) => <Row key={v.id} v={v} onPick={pick} />)}
            </>
          )}
        </div>
      )}
    </div>
  );
}

/* The plan that holds a session's queued tasks: pinned first, one click opens it. */
function Holding({ v, onPick }: { v: VisualDTO; onPick(id: string): void }) {
  const n = v.holding ?? 0;
  return (
    <button type="button" className="mb-1 flex min-w-0 flex-col gap-0.5 rounded-md border-l-2 border-sap bg-sap/10 px-s1 py-1.5 text-left hover:bg-sap/15" data-testid={`visual-holding-${v.id}`} onClick={() => onPick(v.id)}>
      <span className="flex items-center justify-between gap-s1">
        <span className="rounded-full bg-sap/15 px-1.5 text-[11px] font-medium text-sap">Holding {n} {n === 1 ? "task" : "tasks"}</span>
        <span className="text-[11px] text-ink-3">{formatAge(Math.max(0, Date.now() / 1000 - v.created_at))}</span>
      </span>
      <span className="truncate text-sm font-semibold text-ink">{v.title}</span>
      <span className="text-xs text-ink-3">The tasks start when you approve it.</span>
    </button>
  );
}

function Row({ v, onPick }: { v: VisualDTO; onPick(id: string): void }) {
  const picked = v.answer && v.options?.find((o) => o.id === v.answer!.option);
  return (
    <button type="button" className="flex min-w-0 items-start gap-s1 rounded-md px-s1 py-1 text-left hover:bg-surface-2" data-testid={`visual-row-${v.id}`} onClick={() => onPick(v.id)}>
      <span className={`mt-[7px] size-1.5 shrink-0 rounded-full ${v.unread ? "bg-sap" : "bg-transparent"}`} aria-hidden />
      <span className="flex min-w-0 flex-1 flex-col">
        <span className={`truncate text-sm ${v.unread ? "font-semibold text-ink" : "text-ink-2"}`}>{v.title}</span>
        <span className="truncate text-xs text-ink-3">
          {v.by} · {formatAge(Math.max(0, Date.now() / 1000 - v.created_at))}
          {picked && ` · ${picked.label}`}
          {v.answered_in_chat && " · answered in chat"}
          {v.kept && " · kept"}
        </span>
      </span>
    </button>
  );
}
