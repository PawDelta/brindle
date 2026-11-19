import { invoke, isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { useContext, useEffect, useRef, useState } from "react";
import type { VisualDTO } from "../../../api/client";
import { AppCovered } from "../../../components/ReconnectOverlay";
import { computedSnapshot, frameMessage, sameSource, type VisualFrameProps, type VisualSource } from "./frame";

type Delivery = {
  label: string;
  visual: { kind: "revision"; id: string; rev: number } | { kind: "kept"; id: string };
  message?: unknown;
  why?: string;
};

// Labels belong to a revision. Finish closing its previous reader before opening it again,
// including React's effect replay and Back while a native open is still pending. Only the same
// label waits: another visual opens at once, while the one before it is still closing.
const lifecycles = new Map<string, Promise<unknown>>();
const lifecycleOf = (label: string): Promise<unknown> => lifecycles.get(label) ?? Promise.resolve();
function extendLifecycle(label: string, step: Promise<unknown>) {
  const settled = step.catch(() => {});
  lifecycles.set(label, settled);
  void settled.then(() => { if (lifecycles.get(label) === settled) lifecycles.delete(label); });
}

// A reader that goes (Back, another visual) parks its page instead of closing it: the webview
// stays, bound to its own revision as before, at no size, and opening that revision again shows
// it at once instead of building a new webview and loading the page again. A revision never
// changes, so the parked page is still its page. The oldest past PARKED is closed.
export const PARKED = 3;
const parked = new Set<string>();
// The revisions a reader shows now (from its mount, before it has taken its parked page back):
// never evicted, and never built a second time as the spare.
const shown = new Set<string>();
function park(label: string) {
  parked.delete(label);
  parked.add(label);
  for (const oldest of parked) {
    if (parked.size <= PARKED) break;
    if (shown.has(oldest)) continue;
    parked.delete(oldest);
    extendLifecycle(oldest, lifecycleOf(oldest).then(() => invoke("visual_close", { label: oldest })));
  }
}
function unpark(label: string): boolean {
  return parked.delete(label);
}
/* Close every parked page (a test's clean slate). */
export function closeParkedVisuals() {
  if (spare) releaseSpare(spare);
  for (const label of [...parked]) {
    parked.delete(label);
    extendLifecycle(label, lifecycleOf(label).then(() => invoke("visual_close", { label })));
  }
}

// The one spare: the page of the visual the person is likeliest to open next (the newest they
// haven't), built ahead at no size and parked, so opening it shows it at once. Bound to its own
// revision like any other. Building: being opened; claimed: a reader took it (it's that reader's
// now); released: no longer wanted (closed once built, if no reader took it).
type Spare = { label: string; state: "building" | "built" | "claimed" | "released"; unlisten?: () => void };
let spare: Spare | null = null;

function claimSpare(label: string) {
  if (spare?.label !== label) return;
  spare.state = "claimed";
  spare.unlisten?.();
  spare = null;
}

function releaseSpare(it: Spare) {
  if (it.state === "claimed" || it.state === "released") return;
  const built = it.state === "built";
  it.state = "released";
  it.unlisten?.();
  if (spare === it) spare = null;
  if (built && parked.delete(it.label)) {
    extendLifecycle(it.label, lifecycleOf(it.label).then(() => invoke("visual_close", { label: it.label })));
  }
}

/* Build the page of `id` at `rev` ahead of its first open; the one spare, replacing any other.
   Answers the release: the spare's page is closed unless a reader has taken it. */
export function prebuildVisual(id: string, rev: number): () => void {
  const label = `visual:${id}:r${rev}`;
  if (parked.has(label) || shown.has(label)) return () => {};      // already built: a reader's
  if (spare?.label === label) {
    const same = spare;
    return () => releaseSpare(same);
  }
  if (spare) releaseSpare(spare);
  const it: Spare = { label, state: "building" };
  spare = it;
  // Read afresh after each await: a reader may claim it, or the session view release it, meanwhile.
  const now = (): Spare["state"] => it.state;
  extendLifecycle(label, lifecycleOf(label).then(async () => {
    if (now() !== "building") return;
    // The blocker can fail with no reader to hear it: then the spare goes.
    const unlisten = await listen<Delivery>("visual:failed", ({ payload }) => {
      if (payload.label === label && it.state !== "claimed") {
        it.state = "built";
        releaseSpare(it);
      }
    });
    it.unlisten = unlisten;
    if (now() === "released") return unlisten();
    if (now() === "claimed") unlisten();        // the reader hears its own failures
    try {
      await invoke<string>("visual_open", { id, rev, bounds: { x: 0, y: 0, width: 0, height: 0 } });
    } catch {
      unlisten();
      if (spare === it) spare = null;
      it.state = "released";
      return;
    }
    if (now() === "released") {
      return invoke("visual_close", { label });
    }
    park(label);
    if (now() === "building") it.state = "built";
  }));
  return () => releaseSpare(it);
}

/* While a session's visuals are on screen: the newest one the person hasn't opened is built ahead
   (the one spare), and released when it changes or the session view goes. */
export function useSpareVisual(visuals: readonly VisualDTO[] | null) {
  const next = (visuals ?? [])
    .filter((v) => v.kind === "visual" && v.unread)
    .reduce<VisualDTO | null>((newest, v) => (!newest || v.created_at > newest.created_at ? v : newest), null);
  const id = next?.id;
  const rev = next?.rev;
  useEffect(() => {
    if (!id || !rev || !isTauri()) return;
    return prebuildVisual(id, rev);
  }, [id, rev]);
}

export function themeMessage() {
  const style = getComputedStyle(document.documentElement);
  const vars = Object.fromEntries([
    "canvas", "canvas-2", "surface", "surface-2", "line", "line-strong", "ink", "ink-2", "ink-3",
    "fawn", "fawn-soft", "sap", "sap-soft", "pass", "pass-soft", "branch", "branch-soft", "fail", "fail-soft",
    "on-fawn", "on-branch", "on-sap", "thread-in", "thread-top", "thread-mid", "thread-out", "sans", "mono",
  ].map((name) => [`--${name}`, style.getPropertyValue(`--${name}`).trim()]));
  // The app's light or dark (styles.css: data-theme forces one, else the system's, dark by default).
  // The page follows it, data-theme and prefers-color-scheme alike (src-tauri/src/visual_theme.js).
  const forced = document.documentElement.dataset.theme;
  const scheme = forced === "light" || forced === "dark" ? forced
    : window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  return { type: "brindle.theme", scheme, vars };
}

export function NativeFrame({ source, title, onMessage, zoom = 1, zoomAnchor = null, snapshotRequest = 0, covered: menuOver = false }: VisualFrameProps) {
  // Covered by a menu of the reader's, or the whole app by the reconnect overlay: the native page
  // sits above the page's DOM, so it takes no room rather than show (and take input) over them.
  const appCovered = useContext(AppCovered);
  const covered = menuOver || appCovered;
  const host = useRef<HTMLDivElement>(null);
  const posted = useRef(onMessage);
  posted.current = onMessage;
  const snapshot = useRef<(() => void) | null>(null);
  const relayout = useRef<(() => void) | null>(null);
  // The webview's own zoom: its content scales and scrolls inside it, so nothing is clipped.
  const zoomed = useRef<((level: number, anchor?: { x: number; y: number; ratio: number } | null) => void) | null>(null);
  const zoomNow = useRef(zoom);
  zoomNow.current = zoom;
  const coveredNow = useRef(covered);
  coveredNow.current = covered;
  const [error, setError] = useState<string | null>(null);
  const { kind, id } = source;
  const rev = source.kind === "session" ? source.rev : null;
  useEffect(() => {
    const element = host.current!;
    const from: VisualSource = rev === null ? { kind: "kept", id } : { kind: "session", id, rev };
    const expected = rev === null ? `visual:kept:${id}` : `visual:${id}:r${rev}`;
    // The spare built for this revision is this reader's now: nothing releases it from under it.
    claimSpare(expected);
    shown.add(expected);
    let disposed = false;
    let failed = false;
    let closing: Promise<unknown> | undefined;
    // The zoom the page was last given: a page opens at 1, and goes back to 1 when parked (the
    // next reader starts at 1 too).
    let sentZoom = 1;
    setError(null);
    // The native surface covers this DOM, including errors. Remove it before reporting failure.
    // A blocker can fail while visual_open is still returning, so share its eventual close
    // with unmount cleanup and serialize the next reader behind it.
    function close() {
      if (!closing) {
        closing = lifecycleOf(expected).then(async () => {
          const label = await opened.catch(() => null);
          if (!label) return;
          if (failed) return invoke("visual_close", { label });
          // Out of sight, kept for the next open of this revision.
          if (sentZoom !== 1) await invoke("visual_set_zoom", { label, zoom: 1 }).catch(() => undefined);
          const { x, y } = element.getBoundingClientRect();
          await invoke("visual_set_bounds", { label, bounds: { x, y, width: 0, height: 0 } });
          park(label);
        });
        extendLifecycle(expected, closing);
      }
      return closing;
    }
    function fail(e: unknown) {
      if (disposed || failed) return;
      failed = true;
      void close().then(() => { if (!disposed) setError(String(e)); }).catch(() => {});
    }
    // While a menu is open over the page, the native page takes no room: the menu can't be covered.
    const bounds = () => {
      const { x, y, width, height } = element.getBoundingClientRect();
      return coveredNow.current ? { x, y, width: 0, height: 0 } : { x, y, width, height };
    };
    const matches = (delivery: Delivery) => {
      const visual = delivery.visual;
      const actual: VisualSource = visual.kind === "revision" ? { kind: "session", id: visual.id, rev: visual.rev } : visual;
      return !disposed && !failed && delivery.label === expected && sameSource(actual, from);
    };
    const listeners = [
      listen<Delivery>("visual:message", ({ payload }) => {
        if (!matches(payload)) return;
        const message = frameMessage(payload.message);
        if (message?.type !== "brindle.snapshot") return posted.current(payload.message, from);
        // Variables, @media and complex selectors resolve to literal values before export. The
        // reader may have moved on while the frame rendered: a stale snapshot is dropped.
        void computedSnapshot(message.html, element.getBoundingClientRect())
          .then((html) => { if (matches(payload)) posted.current({ ...message, html }, from); });
      }),
      listen<Delivery>("visual:loaded", ({ payload }) => { if (matches(payload)) theme(); }),
      listen<Delivery>("visual:failed", ({ payload }) => { if (matches(payload)) fail(payload.why); }),
    ];
    const registered = Promise.all(listeners);
    const opened = lifecycleOf(expected).then(async () => {
      await registered;
      if (disposed) return null;
      if (unpark(expected)) {
        try {
          await invoke("visual_set_bounds", { label: expected, bounds: bounds() });
          return expected;
        } catch {
          // No longer open (the shell closed it): opened afresh below.
        }
      }
      return invoke<string>("visual_open", { id, rev, bounds: bounds() });
    });
    extendLifecycle(expected, opened);
    let sends: Promise<unknown> = opened;
    const send = (command: string, args: Record<string, unknown> = {}) => {
      sends = sends.then(() => opened).then((label) => {
        if (!disposed && !failed && label) return invoke(command, { label, ...args });
      }).catch(fail);
    };
    const layout = () => send("visual_set_bounds", { bounds: bounds() });
    relayout.current = layout;
    function theme() { send("visual_post", { message: themeMessage() }); }
    snapshot.current = () => send("visual_snapshot");
    zoomed.current = (level, anchor) => {
      if (level === sentZoom) return;
      sentZoom = level;
      send("visual_set_zoom", { zoom: level });
      // The page moves its scroll so the point the person zoomed around stays under the pointer.
      if (anchor) send("visual_post", { message: { type: "brindle.zoom.anchor", x: anchor.x, y: anchor.y, ratio: anchor.ratio } });
    };
    void opened.then(() => { if (!disposed) { layout(); theme(); zoomed.current?.(zoomNow.current); } }, fail);
    // Sidebar/Focus and answer-bar changes alter the content rect without a window resize.
    const resize = new ResizeObserver(layout);
    for (let parent: HTMLElement | null = element; parent; parent = parent.parentElement) resize.observe(parent);
    window.addEventListener("resize", layout);
    window.addEventListener("scroll", layout, true);
    const observer = new MutationObserver(theme);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "style", "class"] });
    const system = window.matchMedia("(prefers-color-scheme: dark)");
    system.addEventListener("change", theme);
    return () => {
      disposed = true;
      shown.delete(expected);
      snapshot.current = null;
      relayout.current = null;
      zoomed.current = null;
      resize.disconnect();
      observer.disconnect();
      system.removeEventListener("change", theme);
      window.removeEventListener("resize", layout);
      window.removeEventListener("scroll", layout, true);
      for (const listener of listeners) void listener.then((unlisten) => unlisten(), () => {});
      void close().catch(() => {});
    };
  }, [kind, id, rev]);
  useEffect(() => { if (snapshotRequest > 0) snapshot.current?.(); }, [snapshotRequest]);
  useEffect(() => { relayout.current?.(); }, [covered]);
  const anchorNow = useRef(zoomAnchor);
  anchorNow.current = zoomAnchor;
  useEffect(() => { zoomed.current?.(zoom, anchorNow.current); }, [zoom]);
  return <div ref={host} className="min-h-0 flex-1" data-testid="visual-frame" aria-label={title}>
    {error && <p role="alert" className="p-s3 text-sm text-sap">Couldn't show this visual: {error}</p>}
  </div>;
}
