// The page's side of the app's theme, run inside the visual prelude (visual_scheme.rs `prelude`).
// Also loaded as it is by the vitest suite (app/src/screens/Session/Visuals/Visuals.test.tsx), so it stays plain JS.
// The host posts {type: "brindle.theme", scheme: "light" | "dark", vars} on load and whenever the app's
// theme changes. The page then follows the app, not the system: its root carries data-theme and
// color-scheme, and every prefers-color-scheme condition in its stylesheets answers the app's scheme.
() => {
  let scheme = null;
  // Each rewritten media list's own text, so a later switch starts again from what the page wrote.
  const original = new WeakMap();
  const QUERY = /\(\s*prefers-color-scheme\s*:\s*(light|dark)\s*\)/gi;
  const answer = (text) => text.replace(QUERY, (_, asked) => asked.toLowerCase() === scheme ? "(min-width: 0px)" : "(max-width: 0px)");
  const rewrite = (media) => {
    if (!media) return;
    const text = original.has(media) ? original.get(media) : media.mediaText;
    if (!/prefers-color-scheme/i.test(text)) return;
    original.set(media, text);
    const wanted = answer(text);
    if (media.mediaText !== wanted) media.mediaText = wanted;
  };
  // Script asks too: matchMedia("(prefers-color-scheme: …)") answers the app's scheme once the host
  // has said it, and its change listeners hear the app's switches. Other queries are the browser's.
  const system = typeof window.matchMedia === "function" ? window.matchMedia.bind(window) : null;
  const listening = new Set();
  if (system) {
    window.matchMedia = function matchMedia(query) {
      const text = String(query);
      if (!/prefers-color-scheme/i.test(text)) return system(text);
      const events = new EventTarget();
      const own = system(text);
      // The query as each app scheme answers it, as live browser lists: the rest of it (a width,
      // say) still changes them, and the one in force tells this list.
      const answered = {};
      const native = () => {
        if (!scheme) return own;
        if (!answered[scheme]) {
          const forScheme = scheme;
          answered[forScheme] = system(text.replace(QUERY, (_, wanted) => wanted.toLowerCase() === forScheme ? "(min-width: 0px)" : "(max-width: 0px)"));
          answered[forScheme].addEventListener?.("change", () => { if (scheme === forScheme) changed(); });
        }
        return answered[scheme];
      };
      const now = () => native().matches;
      const list = {
        media: own.media,
        get matches() { return now(); },
        onchange: null,
        addEventListener: (type, fn, options) => events.addEventListener(type, fn, options),
        removeEventListener: (type, fn, options) => events.removeEventListener(type, fn, options),
        dispatchEvent: (event) => events.dispatchEvent(event),
        addListener: (fn) => events.addEventListener("change", fn),
        removeListener: (fn) => events.removeEventListener("change", fn),
      };
      let last = now();
      const changed = () => {
        const matches = now();
        if (matches === last) return;
        last = matches;
        const event = new Event("change");
        Object.defineProperties(event, { matches: { value: matches }, media: { value: list.media } });
        if (typeof list.onchange === "function") list.onchange.call(list, event);
        events.dispatchEvent(event);
      };
      // The system's own switch still counts until the host has said the app's scheme.
      own.addEventListener?.("change", () => { if (!scheme) changed(); });
      listening.add(changed);
      return list;
    };
  }
  const visit = (rules) => {
    for (const rule of Array.from(rules)) {
      rewrite(rule.media);
      if (rule.cssRules) visit(rule.cssRules);
    }
  };
  const apply = () => {
    if (!scheme) return;
    const root = document.documentElement;
    if (root) {
      if (root.getAttribute("data-theme") !== scheme) root.setAttribute("data-theme", scheme);
      if (root.style.getPropertyValue("color-scheme") !== scheme) root.style.setProperty("color-scheme", scheme);
    }
    for (const sheet of Array.from(document.styleSheets)) {
      rewrite(sheet.media);
      try { visit(sheet.cssRules); } catch (_) { /* a sheet the page can't read has nothing to rewrite */ }
    }
    // <picture> sources and the like choose by media too.
    for (const source of Array.from(document.querySelectorAll("source[media], link[media], style[media]"))) {
      const text = source.getAttribute("data-brindle-media") ?? source.getAttribute("media");
      if (!/prefers-color-scheme/i.test(text)) continue;
      source.setAttribute("data-brindle-media", text);
      const wanted = answer(text);
      if (source.getAttribute("media") !== wanted) source.setAttribute("media", wanted);
    }
  };
  // Stylesheets the page adds later (or a script resetting the root) are brought in line as well.
  let queued = false;
  const later = () => {
    if (queued || !scheme) return;
    queued = true;
    Promise.resolve().then(() => { queued = false; apply(); });
  };
  new MutationObserver(later).observe(document, { subtree: true, childList: true, attributes: true, attributeFilter: ["data-theme", "style", "media"] });
  document.addEventListener("load", later, true);
  window.addEventListener("message", (event) => {
    const m = event.data;
    if (event.origin !== "brindle://host" || !m || m.type !== "brindle.theme") return;
    if (m.scheme !== "light" && m.scheme !== "dark") return;
    scheme = m.scheme;
    apply();
    for (const changed of listening) changed();
  });
}
