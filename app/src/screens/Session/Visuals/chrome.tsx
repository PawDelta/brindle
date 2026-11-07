/* The readers' shared chrome: Back (Esc) and Keep, for a framed visual and a plan alike. */

import { useEffect, useRef, useState } from "react";
import type { VisualDTO } from "../../../api/client";
import { plain } from "../../../api/wording";
import { LoadingButton } from "../../../components/LoadingButton";
import { ArrowLeftIcon, BookmarkIcon } from "../../../components/icons";
import { button } from "../../../components/ui";
import type { VisualsClient } from "./client";

export const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

/* Esc goes back, unless a field or menu took it first. In a field, Esc first lets go of it, so a
   half-typed note isn't lost. */
export function useEscBack(onBack: () => void) {
  const back = useRef(onBack);
  back.current = onBack;
  useEffect(() => {
    const esc = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      const typing = e.target instanceof HTMLElement && e.target.closest("input, textarea");
      if (typing) (typing as HTMLElement).blur();
      else back.current();
    };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, []);
  return back;
}

/* Back (Esc), with the sap badge when the agent asks something new while this is open. */
export function BackButton({ onBack, label = "Back", asking }: { onBack(): void; label?: string; asking?: boolean }) {
  return (
    <button type="button" className={`${button} relative pl-2 pr-2`} onClick={onBack} title={`${label} (Esc)`} data-testid="visual-back">
      <ArrowLeftIcon size={15} className="text-ink-2" />
      {label}
      <kbd className="ml-0.5 rounded-[3px] border border-line bg-surface-2 px-1 font-mono text-[10px] font-medium leading-4 text-ink-3">Esc</kbd>
      {asking && <span className="absolute -right-1 -top-1 size-2.5 rounded-full bg-sap ring-2 ring-canvas" aria-label="the agent asks something new" data-testid="visual-back-asking" />}
    </button>
  );
}

/* Keep / Kept, for any visual of the session (a plan too). */
export function KeepButton({ client, visual, onKept }: { client: VisualsClient; visual: VisualDTO; onKept(v: VisualDTO): void }) {
  const [keeping, setKeeping] = useState(false);
  const [keepError, setKeepError] = useState<string | null>(null);
  const keep = async () => {
    setKeeping(true);
    setKeepError(null);
    try {
      onKept(await (visual.kept ? client.unkeepVisual(visual.id) : client.keepVisual(visual.id)));
    } catch (e) {
      setKeepError(message(e));
    } finally {
      setKeeping(false);
    }
  };
  return (
    <>
      {keepError && <span className="text-xs text-sap">{keepError}</span>}
      <LoadingButton className={`${button} pl-2.5 ${visual.kept ? "border-branch/45 bg-branch-soft" : ""}`} busy={keeping} disabled={keeping} aria-pressed={visual.kept} title={visual.kept ? "Kept in Knowledge › Kept visuals (click to remove)" : "Keep a copy in Knowledge › Kept visuals"} data-testid="visual-keep" onClick={() => void keep()}>
        <BookmarkIcon size={15} filled={visual.kept} className={visual.kept ? "text-branch" : "text-ink-2"} />
        {visual.kept ? "Kept" : "Keep"}
      </LoadingButton>
    </>
  );
}
