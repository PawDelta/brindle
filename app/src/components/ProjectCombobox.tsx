/* The project a session starts in: type to filter the projects, arrow keys
   and Enter to pick one. A click outside it, or Escape, closes the list. */

import { useEffect, useId, useRef, useState } from "react";
import type { Repo } from "../api/client";
import { field } from "./ui";

export function ProjectCombobox({ repos, value, onChange }: { repos: Repo[]; value: string; onChange(repo: string): void }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const box = useRef<HTMLDivElement>(null);
  const listId = useId();

  const needle = query.trim().toLowerCase();
  const shown = repos.filter((r) => r.name.toLowerCase().includes(needle) || r.repo_root.toLowerCase().includes(needle));
  const selected = repos.find((r) => r.repo_root === value);

  useEffect(() => {
    if (!open) return;
    const outside = (e: MouseEvent) => {
      if (!box.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", outside);
    return () => document.removeEventListener("mousedown", outside);
  }, [open]);

  // The highlighted option stays in view as the arrow keys move it.
  useEffect(() => {
    if (open) document.getElementById(`${listId}-${active}`)?.scrollIntoView?.({ block: "nearest" });
  }, [open, active, listId]);

  const show = () => {
    setQuery("");
    setActive(0);
    setOpen(true);
  };
  const pick = (repo: string) => {
    onChange(repo);
    setOpen(false);
  };

  return (
    <div
      ref={box}
      className="relative"
      onBlur={(e) => {
        if (!box.current?.contains(e.relatedTarget as Node | null)) setOpen(false);
      }}
    >
      <input
        className={field}
        role="combobox"
        aria-label="Repo"
        placeholder="Search projects…"
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={open && shown[active] ? `${listId}-${active}` : undefined}
        autoComplete="off"
        value={open ? query : (selected?.name ?? "")}
        onFocus={show}
        onClick={() => !open && show()}
        onChange={(e) => {
          setQuery(e.target.value);
          setActive(0);
          setOpen(true);
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            if (open) e.stopPropagation();
            setOpen(false);
          } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            if (!open) show();
            else if (shown.length) setActive((a) => (a + (e.key === "ArrowDown" ? 1 : -1) + shown.length) % shown.length);
          } else if (e.key === "Enter" && open) {
            e.preventDefault();
            if (shown[active]) pick(shown[active].repo_root);
          }
        }}
      />
      {open && (
        <ul id={listId} role="listbox" aria-label="Projects" className="absolute z-10 mt-1 max-h-60 w-max min-w-64 max-w-[28rem] overflow-auto rounded-md border border-line bg-surface py-0.5 shadow-lg">
          {shown.length === 0 && <li className="px-s1 py-0.5 text-sm text-ink-3">No project matches</li>}
          {shown.map((r, i) => (
            <li
              key={r.repo_root}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={r.repo_root === value}
              title={r.name}
              className={`cursor-pointer truncate whitespace-nowrap px-s1 py-0.5 text-sm ${i === active ? "bg-surface-2 text-ink" : "text-ink-2"}`}
              onMouseEnter={() => setActive(i)}
              onMouseDown={(e) => {
                e.preventDefault();
                pick(r.repo_root);
              }}
            >
              {r.name}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
