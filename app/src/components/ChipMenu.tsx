/* A chip in a composer's toolbar that opens a menu under it (New session's
   project and CLI chips): the chip says what is picked, the menu lists the
   choices (the picked one ticked) and any actions after a rule. ↑ ↓ move
   through the items, Enter or a click picks one, Escape or a click outside
   closes it and focus goes back to the chip. */

import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { CheckIcon } from "./icons";

export interface ChipMenuItem {
  key: string;
  label: ReactNode;
  /* Picked (ticked) or not, for a choice; left out for an action. */
  checked?: boolean;
  /* An action's icon, in the tick's place. */
  icon?: ReactNode;
  /* Quiet words at the item's right (a project's running count, "signed in"). */
  detail?: ReactNode;
  onSelect(): void;
}

/* A rule between the choices and the actions. */
export const SEPARATOR = "separator" as const;

export const CHIP = "inline-flex h-7 items-center gap-1.5 whitespace-nowrap rounded-[var(--r)] border px-2.5 text-[13px] text-ink bg-ink/5 hover:bg-ink/8 focus-visible:outline-2 focus-visible:outline-branch disabled:opacity-50";

export function ChipMenu({ label, value, chip, heading, items, footer, width = 320, disabled = false, tone }: {
  /* The menu's accessible name; the chip's is "<label>: <value>". */
  label: string;
  value: string;
  /* What the chip shows: the pick, with its icon. */
  chip: ReactNode;
  /* A small heading over the items. */
  heading?: string;
  items: (ChipMenuItem | typeof SEPARATOR)[];
  footer?: ReactNode;
  width?: number;
  disabled?: boolean;
  /* "ask": nothing picked yet, the chip asks for a pick. */
  tone?: "ask";
}) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLSpanElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const id = useId();

  useEffect(() => {
    if (!open) return;
    const outside = (e: MouseEvent) => {
      if (!box.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", outside);
    // The picked item, else the first, takes focus: the arrow keys start there.
    const entries = [...(menu.current?.querySelectorAll<HTMLElement>("[role^=menuitem]") ?? [])];
    (entries.find((el) => el.getAttribute("aria-checked") === "true") ?? entries[0])?.focus();
    return () => document.removeEventListener("mousedown", outside);
  }, [open]);

  const close = () => {
    setOpen(false);
    button.current?.focus();
  };
  const keys = (e: KeyboardEvent) => {
    const entries = [...(menu.current?.querySelectorAll<HTMLElement>("[role^=menuitem]") ?? [])];
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

  return (
    <span ref={box} className="relative inline-flex">
      <button
        ref={button}
        type="button"
        className={`${CHIP} ${open ? "border-branch" : tone === "ask" ? "border-sap/50" : "border-transparent"}`}
        aria-label={`${label}: ${value}`}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" && !open) {
            e.preventDefault();
            setOpen(true);
          }
        }}
      >
        {chip}
        <span className="text-[10px] text-ink-3" aria-hidden>
          ▾
        </span>
      </button>
      {open && (
        <div
          ref={menu}
          id={id}
          role="menu"
          aria-label={label}
          className="absolute left-0 top-[calc(100%+8px)] z-20 flex flex-col rounded-[var(--r)] border border-line-strong bg-surface-2 p-1.5 shadow-xl"
          style={{ width }}
          onKeyDown={keys}
        >
          {heading && <div className="px-2.5 pb-1 pt-1.5 text-[11px] font-semibold uppercase tracking-[0.08em] text-ink-3">{heading}</div>}
          {items.map((item, i) =>
            item === SEPARATOR ? (
              <div key={`sep-${i}`} role="separator" className="mx-1 my-1.5 h-px bg-line" />
            ) : (
              <button
                key={item.key}
                type="button"
                role={item.checked === undefined ? "menuitem" : "menuitemradio"}
                aria-checked={item.checked}
                className={`flex w-full items-center gap-2.5 whitespace-nowrap rounded-[var(--r)] px-2.5 py-[7px] text-left text-[13px] text-ink outline-none hover:bg-ink/6 focus-visible:bg-ink/6 ${item.checked ? "bg-branch-soft" : ""}`}
                onClick={() => {
                  close();
                  item.onSelect();
                }}
              >
                <span className="flex w-4 shrink-0 justify-center" aria-hidden>
                  {item.checked ? <CheckIcon /> : item.icon}
                </span>
                <span className="max-w-[70%] shrink-0 truncate">{item.label}</span>
                {item.detail && <span className="ml-auto min-w-0 truncate pl-4 text-[11.5px] text-ink-3">{item.detail}</span>}
              </button>
            ),
          )}
          {footer && <div className="px-2.5 pb-1 pt-1.5 text-[11.5px] text-ink-3">{footer}</div>}
        </div>
      )}
    </span>
  );
}
