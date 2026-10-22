/* Timing probes for the terminal's wheel (e2e/pty.e2e.ts's smooth test, over
   the pty view's bridge socket): when each wheel event came, each frame went
   up the socket and came down it, and each time xterm.js drew rows; and a trackpad swipe, as
   macOS hands one to a web view: a wheel event a frame, in pixels, the
   finger's push then the momentum's decay. Times are epoch milliseconds
   (performance.timeOrigin + now), to line up with the engine's log. */

import { Terminal as XTerm } from "@xterm/xterm";
import type { WebSocketFactory, WebSocketLike } from "../src/api/events";

export interface Probe {
  wheels: { t: number; dy: number }[];
  sends: { t: number; text: string }[];
  recvs: { t: number; bytes: number }[];
  /* When a frame came down that ends a synchronized update (DEC 2026): a
     redraw, all of it in (tmux brackets each of its redraws so for a client
     that has the "sync" feature, as the view's has). */
  ends: number[];
  renders: { t: number; start: number; end: number }[];
  /* The bytes the terminal was handed, as text, for reading what it shows. */
  terms: XTerm[];
}

export const now = () => performance.timeOrigin + performance.now();

const latin1 = new TextDecoder("latin1");

export function installProbe(): Probe {
  const probe: Probe = { wheels: [], sends: [], recvs: [], ends: [], renders: [], terms: [] };
  (window as unknown as { probe: Probe }).probe = probe;
  addEventListener("wheel", (e) => probe.wheels.push({ t: now(), dy: e.deltaY }), { capture: true });
  // Every xterm opened from here on reports the rows it draws (in the frame it paints them).
  const open = XTerm.prototype.open;
  XTerm.prototype.open = function (this: XTerm, parent: HTMLElement) {
    probe.terms.push(this);
    this.onRender(({ start, end }) => probe.renders.push({ t: now(), start, end }));
    return open.call(this, parent);
  };
  return probe;
}

/* A socket factory that logs what goes up and comes down. */
export function probeSocket(probe: Probe, connect?: WebSocketFactory): WebSocketFactory {
  return (url) => {
    let inner: WebSocketLike;
    if (connect) inner = connect(url);
    else {
      const ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
      inner = ws as unknown as WebSocketLike;
    }
    const outer: WebSocketLike = {
      send(data) {
        probe.sends.push({ t: now(), text: data });
        inner.send(data);
      },
      close: (code, reason) => inner.close(code, reason),
      onopen: null,
      onmessage: null,
      onclose: null,
      onerror: null,
    };
    inner.onopen = (ev) => outer.onopen?.(ev);
    inner.onclose = (ev) => outer.onclose?.(ev);
    inner.onerror = (ev) => outer.onerror?.(ev);
    inner.onmessage = (ev) => {
      const d = ev.data;
      if (typeof d !== "string") {
        const bytes = d instanceof ArrayBuffer ? new Uint8Array(d) : (d as Uint8Array);
        const t = now();
        probe.recvs.push({ t, bytes: bytes.byteLength });
        if (latin1.decode(bytes).includes("\x1b[?2026l")) probe.ends.push(t);
      }
      outer.onmessage?.(ev);
    };
    return outer;
  };
}

/* The pixel deltas of one trackpad swipe, a frame apart: up (negative) by
   default. The finger's push grows to `peak`, then momentum decays by
   `decay` a frame until under half a pixel. */
export function swipeDeltas(peak = 48, decay = 0.94, up = true): number[] {
  const push = [0.05, 0.12, 0.25, 0.4, 0.6, 0.8, 0.95, 1].map((f) => f * peak);
  const out = [...push];
  for (let v = peak; v >= 0.5; v *= decay) out.push(v);
  return out.map((d) => Math.round((up ? -d : d) * 10) / 10);
}

/* Turns the wheel over `target`, `deltas` in pixels, `perFrame` events a
   frame (a 120 Hz trackpad's two to a 60 Hz page's frame), as WebKit
   dispatches a trackpad's; resolves once the last is dispatched. */
export function swipe(target: Element, deltas: number[], perFrame = 1): Promise<void> {
  const rect = target.getBoundingClientRect();
  const clientX = rect.left + rect.width / 2;
  const clientY = rect.top + rect.height / 3;
  return new Promise((resolve) => {
    let i = 0;
    const step = () => {
      if (i >= deltas.length) return resolve();
      for (let k = 0; k < perFrame && i < deltas.length; k++) {
        target.dispatchEvent(new WheelEvent("wheel", { deltaY: deltas[i++], deltaMode: 0, clientX, clientY, bubbles: true, cancelable: true }));
      }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
}
