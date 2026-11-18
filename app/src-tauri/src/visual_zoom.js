// The page's side of zoom, run inside the visual prelude (visual_scheme.rs `prelude`) with its `send`.
// Also loaded as it is by the browser e2e (app/e2e/visual-zoom.e2e.ts), so it stays plain JS.
(send) => {
  // Zoom keys and pinch / ⌘-scroll inside the page never reach the host: forward them as brindle.zoom.
  document.addEventListener("keydown", (event) => {
    if (!(event.metaKey || event.ctrlKey) || event.altKey) return;
    const dir = event.key === "+" || event.key === "=" ? "in" : event.key === "-" || event.key === "_" ? "out" : event.key === "0" ? "reset" : null;
    if (dir) { event.preventDefault(); send({ type: "brindle.zoom", dir }); }
  }, true);
  window.addEventListener("wheel", (event) => {
    if (!(event.ctrlKey || event.metaKey) || !event.deltaY) return;
    event.preventDefault();
    const factor = Math.min(1.25, Math.max(0.8, Math.exp(-event.deltaY * 0.01)));
    send({ type: "brindle.zoom", dir: event.deltaY < 0 ? "in" : "out", factor, x: event.clientX, y: event.clientY });
  }, { passive: false, capture: true });
  // WebKit reports a trackpad pinch as gesture events: the scale since the gesture began.
  let pinched = 1;
  for (const name of ["gesturestart", "gesturechange", "gestureend"]) {
    document.addEventListener(name, (event) => {
      event.preventDefault();
      if (name === "gesturestart") { pinched = 1; return; }
      if (name === "gestureend" || !(event.scale > 0)) return;
      const factor = event.scale / pinched;
      pinched = event.scale;
      if (factor !== 1) send({ type: "brindle.zoom", dir: factor > 1 ? "in" : "out", factor, x: event.clientX, y: event.clientY });
    }, { passive: false });
  }
  // After a zoom the host says which point the person zoomed around: keep it under the pointer.
  window.addEventListener("message", (event) => {
    const m = event.data;
    if (event.origin !== "brindle://host" || !m || m.type !== "brindle.zoom.anchor") return;
    const [x, y, ratio] = [m.x, m.y, m.ratio];
    if (![x, y, ratio].every(Number.isFinite)) return;
    requestAnimationFrame(() => window.scrollBy(x * (1 - ratio), y * (1 - ratio)));
  });
}
