/* The first-time walkthrough: a few coach marks over the app that point at
   the controls a new person needs (New session, the project picker, the
   Inbox, Account and Settings, the session view), one at a time, with Next,
   Back and Skip. It opens by itself once, right after the first launch's
   setup (App's FirstLaunch passes `autoStart`), and again whenever
   openWalkthrough() is called. Finished or skipped, it is remembered on this Mac and
   never opens by itself again.

   Each step finds its control by a data attribute, data-walkthrough="<id>",
   brings it into view, and follows it as it moves or scrolls. A step whose
   control isn't on screen still shows, centred, without a highlight. */

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { main, panel, quiet } from "./ui";

export const WALKTHROUGH_KEY = "brindle.walkthrough";
const OPEN_EVENT = "brindle:walkthrough";

export interface WalkthroughStep {
  /* The data-walkthrough value of the control this step points at. */
  target: string;
  title: string;
  body: string;
}

export const STEPS: WalkthroughStep[] = [
  {
    target: "new-session",
    title: "Start a session here",
    body: "New session opens the composer: describe what brindle should work on and press Start. A supervisor plans the work and hands it to agents, each on its own branch.",
  },
  {
    target: "project-picker",
    title: "Pick the project",
    body: "A session runs in a project: a git repository on this Mac. Pick one here, or add another folder or clone one.",
  },
  {
    target: "inbox",
    title: "Your Inbox",
    body: "Anything that needs you, across every session (a permission, a plan to approve, a review), waits here.",
  },
  {
    target: "profile",
    title: "Agents, Settings and the Library",
    body: "Your initials open Settings: your account (plan, sign-in and usage), your agent profiles, the coding CLIs, keys and permission rules. The button beside them opens the Library: your documents and decisions.",
  },
  {
    target: "session-view",
    title: "The session view",
    body: "A running session fills this area with its supervisor's conversation; its agents are listed in the sidebar.",
  },
];

export function readWalkthroughDone(): boolean {
  try {
    return localStorage.getItem(WALKTHROUGH_KEY) === "done";
  } catch {
    return false;
  }
}

function markWalkthroughDone(): void {
  try {
    localStorage.setItem(WALKTHROUGH_KEY, "done");
  } catch {
    // Storage refused: closed for this launch only.
  }
}

/* Open the walkthrough from its first step, wherever it is mounted. */
export function openWalkthrough(): void {
  window.dispatchEvent(new Event(OPEN_EVENT));
}

function findTarget(step: WalkthroughStep): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[data-walkthrough="${step.target}"]`);
}

interface Box {
  top: number;
  left: number;
  width: number;
  height: number;
}

const PAD = 4;
const CARD_W = 320;
const GAP = 12;

/* The part of the control a person can see: its box cut to the window and to
   every ancestor that clips its overflow (a scrolling column under a banner);
   null when nothing of it shows, and the card shows centred instead. */
function measure(el: HTMLElement | null): Box | null {
  const r = el?.getBoundingClientRect();
  if (!el || !r || r.width === 0 || r.height === 0) return null;
  let top = Math.max(r.top, 0);
  let left = Math.max(r.left, 0);
  let bottom = Math.min(r.bottom, window.innerHeight);
  let right = Math.min(r.right, window.innerWidth);
  for (let a = el.parentElement; a && a !== document.body && a !== document.documentElement; a = a.parentElement) {
    // Each axis clips on its own.
    const { overflowX, overflowY } = getComputedStyle(a);
    const clipsX = overflowX !== "visible";
    const clipsY = overflowY !== "visible";
    if (!clipsX && !clipsY) continue;
    const c = a.getBoundingClientRect();
    if (clipsY) {
      top = Math.max(top, c.top);
      bottom = Math.min(bottom, c.bottom);
    }
    if (clipsX) {
      left = Math.max(left, c.left);
      right = Math.min(right, c.right);
    }
  }
  if (bottom <= top || right <= left) return null;
  return { top: top - PAD, left: left - PAD, width: right - left + PAD * 2, height: bottom - top + PAD * 2 };
}

/* The card beside its control: to the right when there's room (the sidebar's
   controls), else under it (or over it, near the bottom); centred over a
   control wider than half the window (the session view), or with none. */
function cardPlace(box: Box | null): CSSProperties {
  const w = window.innerWidth;
  const h = window.innerHeight;
  const clampX = (x: number) => Math.max(GAP, Math.min(x, w - CARD_W - GAP));
  const clampY = (y: number) => Math.max(GAP, Math.min(y, h - 200));
  if (!box || box.width > w / 2) {
    const cx = box ? box.left + box.width / 2 : w / 2;
    const cy = box ? box.top + box.height / 2 : h / 2;
    return { left: clampX(cx - CARD_W / 2), top: clampY(cy - 80) };
  }
  if (box.left + box.width + GAP + CARD_W <= w) return { left: box.left + box.width + GAP, top: clampY(box.top) };
  const below = box.top + box.height + GAP;
  return { left: clampX(box.left), top: below + 180 <= h ? below : clampY(box.top - GAP - 180) };
}

export function Walkthrough({ autoStart = false, onStep }: { autoStart?: boolean; onStep?: (step: WalkthroughStep) => boolean | void }) {
  // A fresh cursor also restarts navigation when Help is pressed on the first step.
  const [cursor, setCursor] = useState<{ index: number } | null>(() => (autoStart && !readWalkthroughDone() ? { index: 0 } : null));
  const step = cursor?.index ?? null;
  const [box, setBox] = useState<Box | null>(null);
  const next = useRef<HTMLButtonElement>(null);
  const navigated = useRef<typeof cursor>(null);

  useEffect(() => {
    const open = () => setCursor({ index: 0 });
    window.addEventListener(OPEN_EVENT, open);
    return () => window.removeEventListener(OPEN_EVENT, open);
  }, []);

  const close = useCallback(() => {
    markWalkthroughDone();
    setCursor(null);
  }, []);

  // App owns the pages: reveal the step's page even if its target isn't mounted yet.
  // Only an explicitly deferred step can revisit when its initial data arrives.
  // Ordinary feed updates must preserve the person's subsequent tab/agent selection.
  useLayoutEffect(() => {
    if (cursor && navigated.current !== cursor && onStep?.(STEPS[cursor.index]) !== false) {
      navigated.current = cursor;
    }
  }, [cursor, onStep]);

  // Follow the control: it can mount late (the composer once the projects load), move, resize, or
  // scroll with any scrolling ancestor (a scroll doesn't bubble: the capture phase sees every one).
  useLayoutEffect(() => {
    if (step === null) return;
    const current = STEPS[step];
    let shown: HTMLElement | null = null;
    const update = () => {
      const el = findTarget(current);
      // Brought into view once per step, when it is first found (jsdom has no scrollIntoView).
      if (el && el !== shown) {
        shown = el;
        el.scrollIntoView?.({ block: "nearest", inline: "nearest" });
      }
      setBox((old) => {
        const now = measure(el);
        const same = old === now || (old && now && old.top === now.top && old.left === now.left && old.width === now.width && old.height === now.height);
        return same ? old : now;
      });
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    const watch = new MutationObserver(update);
    watch.observe(document.body, { childList: true, subtree: true });
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
      watch.disconnect();
    };
  }, [step, cursor]);

  useEffect(() => {
    if (step === null) return;
    next.current?.focus();
    const keys = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", keys);
    return () => window.removeEventListener("keydown", keys);
  }, [step, close]);

  if (step === null) return null;
  const current = STEPS[step];
  const last = step === STEPS.length - 1;
  return (
    <div className="pointer-events-none fixed inset-0 z-50" data-testid="walkthrough" data-step={current.target}>
      {box ? (
        <div
          aria-hidden
          data-testid="walkthrough-highlight"
          className="absolute rounded-lg ring-2 ring-branch transition-all duration-150"
          style={{ ...box, boxShadow: "0 0 0 9999px rgb(0 0 0 / 0.4)" }}
        />
      ) : (
        <div aria-hidden className="absolute inset-0 bg-black/40" />
      )}
      <section
        role="dialog"
        aria-label="Walkthrough"
        aria-describedby="walkthrough-body"
        className={`${panel} pointer-events-auto absolute flex flex-col gap-s1 p-s2 shadow-lg`}
        style={{ ...cardPlace(box), width: CARD_W }}
      >
        <div className="text-xs text-ink-3">
          {step + 1} of {STEPS.length}
        </div>
        <h2 className="text-[15px] font-semibold">{current.title}</h2>
        <p id="walkthrough-body" className="text-sm text-ink-2">
          {current.body}
        </p>
        <div className="flex items-center gap-s1 pt-s1">
          <button type="button" className={quiet} onClick={close}>
            Skip
          </button>
          <span className="flex-1" />
          {step > 0 && (
            <button type="button" className={quiet} onClick={() => setCursor({ index: step - 1 })}>
              Back
            </button>
          )}
          <button ref={next} type="button" className={main} onClick={() => (last ? close() : setCursor({ index: step + 1 }))}>
            {last ? "Done" : "Next"}
          </button>
        </div>
      </section>
    </div>
  );
}
