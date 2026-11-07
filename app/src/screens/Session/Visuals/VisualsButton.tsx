/* The session header's way in: an icon, with a dot while one is unread. It opens the session's
   list: the open ones first, newest first, and the
   decided ones folded under "Decided · N". Picking one opens it in the chat's place. No visuals (or
   an engine without them): no button. */

import { useEffect, useRef, useState } from "react";
import type { VisualDTO } from "../../../api/client";
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
  const pending = visuals.filter((v) => !decided(v));
  const done = visuals.filter(decided);
  const pick = (id: string) => {
    setOpen(false);
    onOpen(id);
  };
  return (
    <div ref={box} className="relative">
      <button type="button" className={`${button} relative !gap-0 p-1.5 text-ink-2 hover:bg-sap/10 hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sap`} aria-label="Visuals" title="Visuals" aria-expanded={open} aria-haspopup="true" data-testid="visuals-button" onClick={() => setOpen((o) => !o)}>
        <svg aria-hidden="true" viewBox="0 0 20 20" className="size-4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M4 2.75h8l4 4v10.5H4z" />
          <path d="M12 2.75v4h4M7 14v-2m3 2V9m3 5v-4" />
        </svg>
      </button>
      {open && (
        <div className={`${panel} absolute right-0 top-full z-20 mt-1 flex w-80 flex-col gap-px p-1`} role="group" aria-label="Visuals" data-testid="visuals-panel">
          {pending.length === 0 && <p className="px-s1 py-1 text-xs text-ink-3">Nothing waits on you here.</p>}
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
          {v.kept && " · kept"}
        </span>
      </span>
    </button>
  );
}
