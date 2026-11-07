import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { useEffect, useRef, useState } from "react";
import { sameSource, type VisualFrameProps, type VisualSource } from "./frame";

type Delivery = {
  label: string;
  visual: { kind: "revision"; id: string; rev: number } | { kind: "kept"; id: string };
  message?: unknown;
  why?: string;
};

// Labels belong to a revision. Finish closing its previous reader before opening it again,
// including React's effect replay and Back while a native open is still pending.
let lifecycle: Promise<unknown> = Promise.resolve();

function themeMessage() {
  const style = getComputedStyle(document.documentElement);
  const vars = Object.fromEntries([
    "canvas", "canvas-2", "surface", "surface-2", "line", "line-strong", "ink", "ink-2", "ink-3",
    "fawn", "fawn-soft", "sap", "sap-soft", "pass", "pass-soft", "branch", "branch-soft", "fail", "fail-soft",
    "on-fawn", "on-branch", "on-sap", "thread-in", "thread-top", "thread-mid", "thread-out", "sans", "mono",
  ].map((name) => [`--${name}`, style.getPropertyValue(`--${name}`).trim()]));
  return { type: "brindle.theme", vars };
}

export function NativeFrame({ source, title, onMessage, snapshotRequest = 0 }: VisualFrameProps) {
  const host = useRef<HTMLDivElement>(null);
  const posted = useRef(onMessage);
  posted.current = onMessage;
  const snapshot = useRef<(() => void) | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { kind, id } = source;
  const rev = source.kind === "session" ? source.rev : null;
  useEffect(() => {
    const element = host.current!;
    const from: VisualSource = rev === null ? { kind: "kept", id } : { kind: "session", id, rev };
    const expected = rev === null ? `visual:kept:${id}` : `visual:${id}:r${rev}`;
    let disposed = false;
    setError(null);
    const fail = (e: unknown) => { if (!disposed) setError(String(e)); };
    const bounds = () => {
      const { x, y, width, height } = element.getBoundingClientRect();
      return { x, y, width, height };
    };
    const matches = (delivery: Delivery) => {
      const visual = delivery.visual;
      const actual: VisualSource = visual.kind === "revision" ? { kind: "session", id: visual.id, rev: visual.rev } : visual;
      return !disposed && delivery.label === expected && sameSource(actual, from);
    };
    const listeners = [
      listen<Delivery>("visual:message", ({ payload }) => { if (matches(payload)) posted.current(payload.message, from); }),
      listen<Delivery>("visual:loaded", ({ payload }) => { if (matches(payload)) theme(); }),
      listen<Delivery>("visual:failed", ({ payload }) => { if (matches(payload)) fail(payload.why); }),
    ];
    const registered = Promise.all(listeners);
    const opened = lifecycle.then(async () => {
      await registered;
      if (disposed) return null;
      return invoke<string>("visual_open", { id, rev, bounds: bounds() });
    });
    lifecycle = opened.catch(() => {});
    let sends: Promise<unknown> = opened;
    const send = (command: string, args: Record<string, unknown> = {}) => {
      sends = sends.then(() => opened).then((label) => {
        if (!disposed && label) return invoke(command, { label, ...args });
      }).catch(fail);
    };
    const layout = () => send("visual_set_bounds", { bounds: bounds() });
    function theme() { send("visual_post", { message: themeMessage() }); }
    snapshot.current = () => send("visual_snapshot");
    void opened.then(() => { if (!disposed) { layout(); theme(); } }, fail);
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
      snapshot.current = null;
      resize.disconnect();
      observer.disconnect();
      system.removeEventListener("change", theme);
      window.removeEventListener("resize", layout);
      window.removeEventListener("scroll", layout, true);
      for (const listener of listeners) void listener.then((unlisten) => unlisten(), () => {});
      lifecycle = lifecycle.then(async () => {
        const label = await opened;
        if (label) await invoke("visual_close", { label });
      }).catch(() => {});
    };
  }, [kind, id, rev]);
  useEffect(() => { if (snapshotRequest > 0) snapshot.current?.(); }, [snapshotRequest]);
  return <div ref={host} className="min-h-0 flex-1" data-testid="visual-frame" aria-label={title}>
    {error && <p role="alert" className="p-s3 text-sm text-sap">Couldn't show this visual: {error}</p>}
  </div>;
}
