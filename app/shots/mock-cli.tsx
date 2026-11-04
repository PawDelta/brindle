import { createRoot } from "react-dom/client";
import { applyTheme } from "../src/components/ThemeToggle";
import { button, card, field, h2, label, lede, main, quiet, title } from "../src/components/ui";
import "../src/styles.css";

const theme = new URLSearchParams(location.search).get("theme") === "light" ? "light" : "dark";
applyTheme(theme);

function Prompt() {
  return (
    <textarea className={`${field} min-h-24 w-full resize-none`} rows={3} readOnly
      defaultValue="Add a /health endpoint with a test" aria-label="What should brindle work on?" />
  );
}

function State({ n, name, note, children }: { n: number; name: string; note: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[220px_1fr] gap-s3">
      <div className="pt-s2">
        <div className={label}>State {n}</div>
        <div className={`${h2} mt-1`}>{name}</div>
        <p className={`${lede} mt-1`}>{note}</p>
      </div>
      {children}
    </div>
  );
}

function One() {
  return (
    <form className={`${card} flex flex-col gap-s1 px-s2 py-s2`}>
      <Prompt />
      <div className="flex flex-wrap items-center gap-s1">
        <span className="text-sm text-ink-2">in <span className="font-medium text-ink">runbooks</span></span>
        <span className="text-sm text-ink-2">on <span className="font-medium text-ink">Codex</span></span>
        <button type="button" className={quiet}>Sign in to more…</button>
        <button type="button" className={quiet}>Add a project…</button>
        <button type="button" className={quiet}>Options ▾</button>
        <span className="ml-auto text-xs text-ink-3">⌘↵</span>
        <button type="button" className={main}>Start</button>
      </div>
    </form>
  );
}

function Two() {
  return (
    <form className={`${card} flex flex-col gap-s1 px-s2 py-s2`}>
      <Prompt />
      <div className="flex flex-wrap items-center gap-s1">
        <span className="text-sm text-ink-2">in <span className="font-medium text-ink">runbooks</span></span>
        <span className="text-sm text-ink-2">on</span>
        <select className={field} aria-label="Supervisor CLI" defaultValue="">
          <option value="" disabled>Pick a CLI</option>
          <option>Codex</option>
          <option>Claude Code</option>
        </select>
        <button type="button" className={quiet}>Sign in to more…</button>
        <button type="button" className={quiet}>Options ▾</button>
        <span className="ml-auto text-xs text-ink-3">⌘↵</span>
        <button type="button" className={main} disabled>Start</button>
      </div>
      <div className="mt-s1 flex flex-col gap-s1 border-t border-line pt-s2">
        <div className={h2}>Which CLI should run your sessions?</div>
        <p className={lede}>You're signed in to two. brindle remembers the one you pick; change it here any time.</p>
        <div className="flex gap-s1 pt-1">
          <button type="button" className={button}>Codex</button>
          <button type="button" className={button}>Claude Code</button>
        </div>
      </div>
    </form>
  );
}

function None() {
  return (
    <section className={`${card} flex flex-col gap-s2 px-s2 py-s3`}>
      <h1 className={title}>Finish setting up</h1>
      <p className={`${lede} max-w-prose`}>
        brindle runs your sessions on a coding CLI. Sign in to one (Claude Code, Codex, Antigravity or OpenCode) and you can start.
      </p>
      <div><button type="button" className={main}>Sign in to a CLI</button></div>
    </section>
  );
}

createRoot(document.getElementById("root")!).render(
  <main className="min-h-screen bg-bg px-s4 py-s4 text-ink">
    <div className="mx-auto flex max-w-[1000px] flex-col gap-s4">
      <State n={1} name="One CLI can run" note="Codex signed in, Claude missing. Named, no menu, no setup card."><One /></State>
      <State n={2} name="Two or more, first session" note="Start waits for a pick; it's remembered and preselected after."><Two /></State>
      <State n={3} name="Nothing can run" note="Sign-in skipped, or every CLI signed out or blocked. No prompt box."><None /></State>
    </div>
  </main>,
);
