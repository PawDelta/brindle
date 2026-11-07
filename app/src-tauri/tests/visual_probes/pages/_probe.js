// Shared by every probe page (inlined by the harness as {{PROBE}}): where the
// listener is, how an attempt is recorded, and how the page says it's done.
// A "leak" is an attempt that succeeded where the sandbox should have refused
// it; the listener counts what actually arrived.
const PORT = "{{PORT}}";
const HTTP = "http://127.0.0.1:" + PORT;
const WS = "ws://127.0.0.1:" + PORT;
const ORIGIN = "{{ORIGIN}}";
const leaks = [];
const attempts = [];
const errors = [];
window.addEventListener("error", (e) => errors.push(String(e.message)));
window.addEventListener("unhandledrejection", (e) => errors.push("rejection: " + String(e.reason)));
function leak(what) { leaks.push(String(what)); }
const later = (ms, fn) => setTimeout(fn, ms);
// `promise` resolves to a leak description, or to nothing when refused. An
// attempt the webview neither answers nor refuses within a few seconds counts
// as refused (a blocked EventSource or Worker never says so).
function attempt(name, promise) {
  const settled = Promise.race([
    Promise.resolve().then(() => promise),
    new Promise((resolve) => later(4000, () => resolve(""))),
  ]);
  attempts.push(settled.then((v) => { if (v) leak(name + ": " + v); }, () => {}));
}
function done(extra) {
  Promise.allSettled(attempts).then(() => {
    // Markup loads have no promise: a grace period lets a blocked one settle.
    later(800, () => parent.postMessage({ type: "choice", option: "probe-done", leaks: leaks.concat(extra || []), errors }, "*"));
  });
}
