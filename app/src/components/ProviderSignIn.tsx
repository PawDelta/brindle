/* A provider's sign-in, browser first: the button starts the CLI's own
   login on the daemon (POST /v1/providers/{p}/login; its terminal stays
   hidden) and a small card says "Finish signing in in your browser…" with
   a spinner and Cancel. The card reads what the CLI shows (its pane,
   through a tmux client of the app's that no one sees: api/paneText.ts):
   a link it printed can be opened again; a question it asks (which way to
   sign in) shows as buttons, and a code it wants pasted from the browser
   as a small field, each answered through POST /v1/terminals/{id}/input,
   which types the one line the person gave. The provider's state is read
   periodically and when the CLI exits (`check`): "Signed in to <provider> ✓",
   or what went wrong, with the terminal behind "Show details" the whole
   time (PtyTerminal on its pane). A CLI that says its sign-in failed but
   doesn't exit (it waits at its own prompt) fails the card on its words,
   with Try again, which ends it and starts afresh; Cancel and Close end
   the CLI's terminal too, so the next press never finds a stuck one. The
   pane is read once the shell says the pty view can run, which may come
   after the sign-in started; one that can't run here (tmux too old or
   missing) fails the card with why.
   Where the app has no pty views at all (a browser tab), the card still
   checks for completed browser sign-in. The app never sees a password or a token: the browser and
   the CLI handle those. */

import { useCallback, useEffect, useRef, useState } from "react";
import { DaemonError, type ActionTerminal } from "../api/client";
import { useDaemon } from "../api/context";
import { openExternal } from "../api/open";
import { keptLink, PANE_DEAD, readPane, READ_SIZE, type PaneReading } from "../api/paneText";
import { usePtyViews, type PtyViews, type PtyViewTarget } from "../api/ptyView";
import { providerLabel } from "../api/providers";
import { PtyTerminal } from "./PtyTerminal";
import { alert, button, field, primary, quiet } from "./ui";

export interface ProviderSignInProps {
  provider: string;
  /* The launch whose cloud account is being refreshed. */
  loginContext?: Record<string, unknown> | null;
  /* Whether the provider reads as signed in now (the Profiles screen asks GET /routing again). */
  check(): Promise<boolean>;
  /* The sign-in ended (either way): the screen reads its providers again. */
  onDone?(): void;
  /* Start at once, without the button (the inbox's credentials sheet). */
  autoStart?: boolean;
  small?: boolean;
  /* The card is up (a sign-in runs, or just ended) or down again: a screen
     keeps the card in place while it is up, whatever the provider's state. */
  onActive?(active: boolean): void;
  /* Offer "Show details" (the CLI's own pane, read-only): off where a screen shows no terminal at all. */
  details?: boolean;
}

/* How long "Signed in ✓" stays before the card goes. */
const DONE_MS = 2500;
const CHECK_MS = 3000;

/* What the card is doing. */
export type Phase =
  | { kind: "idle" }
  | { kind: "starting" }
  | { kind: "waiting"; terminal: string }
  | { kind: "choose"; terminal: string; question: string; choices: { n: string; label: string }[] }
  | { kind: "code"; terminal: string; prompt: string }
  | { kind: "checking"; terminal: string }
  | { kind: "done"; terminal: string }
  | { kind: "failed"; terminal: string | null; message: string };

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

/* ANSI escapes and carriage returns out: the CLI's words alone. */
const ANSI = /\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\r/g;
export const plainText = (bytes: Uint8Array): string => new TextDecoder().decode(bytes).replace(ANSI, "");

/* What the CLI's last lines ask for: a way to sign in (a numbered list
   after a question), a code to paste, or nothing yet. */
export function readPrompt(text: string): { kind: "choose"; question: string; choices: { n: string; label: string }[] } | { kind: "code"; prompt: string } | null {
  const lines = text.split("\n").map((l) => l.trim());
  const tail = lines.slice(-12);
  const numbered = tail.map((l) => /^[❯>]?\s*(\d)[.)]\s+(.+?)\s*$/.exec(l)).filter((m): m is RegExpExecArray => !!m);
  const question = [...tail].reverse().find((l) => /(select|choose|pick|how (do|would) you|which).*(method|way|account|sign|log)/i.test(l));
  if (numbered.length >= 2 && question) return { kind: "choose", question, choices: numbered.map((m) => ({ n: m[1], label: m[2].replace(/\s*\(.*\)$/, "") })) };
  const code = [...tail].reverse().find((l) => /paste.*code|enter.*code|authorization code|code:\s*$|code here/i.test(l));
  if (code) return { kind: "code", prompt: code };
  return null;
}

/* The line where the CLI says its sign-in failed, if its last lines say so:
   a CLI that fails without exiting (offers another go, waits at its own prompt)
   never ends its pane, so this is the only word the card gets of it. */
export function readFailure(text: string): string | null {
  const tail = text.split("\n").map((l) => l.trim()).slice(-12);
  const failed = /\b(log ?in|sign ?in|oauth|auth\w*|token)\b.*\b(fail\w*|error|denied|invalid|expired|timed? ?out)\b|\b(error|failed)\b.*\b(log ?in|sign ?in|oauth|auth\w*)\b/i;
  return [...tail].reverse().find((l) => failed.test(l) && !PANE_DEAD.test(l)) ?? null;
}

/* The link the CLI printed, if any (opened in the browser by the CLI, or offered here). */
export const readLink = (text: string): string | null => text.match(/https?:\/\/[^\s'"<>]+/g)?.at(-1) ?? null;

/* The sign-in's workings without its card: start it, follow what the CLI
   prints, answer its question or code, cancel. ProviderSignIn below wears
   it as the small card; the first launch (screens/Onboarding) as its own
   full-window step. `onDone` hears whether the provider reads as signed in.
   `pane`: where the CLI's pane is, for "Show details" (with `views`, the
   app's pty views, once they can show it). */
export function useProviderSignIn({ provider, loginContext, check, onDone, onActive, details: offerDetails = true }: Pick<ProviderSignInProps, "provider" | "loginContext" | "check" | "onActive" | "details"> & { onDone?(ok: boolean): void }) {
  const client = useDaemon();
  const name = providerLabel(provider);
  const all = usePtyViews();
  const views = all?.status.kind === "ready" ? all : null;
  const open = views?.open ?? null;
  // The pty view can't run here (tmux too old or missing): nothing can follow the CLI.
  const unusable = all?.status.kind === "unusable" ? all.status.message : null;
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [link, setLink] = useState<string | null>(null);
  // The sign-in running: its terminal's id and where its pane is.
  const [action, setAction] = useState<{ terminal: string; at: PtyViewTarget } | null>(null);
  const finished = useRef(false);
  // The reader at work, and the size it reads at while the details show the pane (null: its own).
  const reader = useRef<PaneReading | null>(null);
  const fitted = useRef<{ cols: number; rows: number } | null>(null);
  const active = useRef(onActive);
  active.current = onActive;
  useEffect(() => {
    active.current?.(phase.kind !== "idle");
  }, [phase.kind]);
  // "Signed in ✓" stays a moment, then the card goes.
  useEffect(() => {
    if (phase.kind !== "done") return;
    const timer = setTimeout(() => setPhase({ kind: "idle" }), DONE_MS);
    return () => clearTimeout(timer);
  }, [phase.kind]);

  // Each attempt's number (a start, a cancel, the hook gone): only the newest one's answers are taken.
  const attempt = useRef(0);
  const pending = useRef<Promise<boolean> | null>(null);
  // A cancelled sign-in's terminal closing: why it couldn't be (kept until a start replaces it), or null once
  // it's gone (or was already).
  const closing = useRef<Promise<string | null>>(Promise.resolve(null));
  const ended = useCallback(
    async (terminal: string, exited = true) => {
      if (finished.current) return;
      const n = attempt.current;
      if (exited) setPhase({ kind: "checking", terminal });
      // An exit waits for the current poll, so only one probe runs at a time.
      const previous = pending.current;
      const reading = previous ?? (pending.current = check().catch(() => false));
      let ok = await reading;
      if (pending.current === reading) pending.current = null;
      if (n !== attempt.current || finished.current) return;
      // A negative poll may have observed credentials before the CLI saved
      // them. Only a check started after exit can declare that login failed.
      if (exited && previous && !ok) {
        const fresh = pending.current ?? (pending.current = check().catch(() => false));
        ok = await fresh;
        if (pending.current === fresh) pending.current = null;
      }
      // Tried again (or cancelled) while it checked: this sign-in's result is no one's now.
      if (n !== attempt.current || finished.current || (!ok && !exited)) return;
      finished.current = true;
      if (ok) setAction(null);
      setPhase(ok ? { kind: "done", terminal } : { kind: "failed", terminal, message: `${name} still isn't signed in.${offerDetails ? " What it said is in the details." : " The browser's sign-in didn't finish, or it signed in to another account."}` });
      onDone?.(ok);
    },
    [check, name, onDone, offerDetails],
  );
  const ending = useRef(ended);
  ending.current = ended;

  /* Start the sign-in. `restart` (Try again): the daemon ends a login of the
     CLI's still running and starts a fresh one, rather than handing back
     the same terminal. A start after a cancel waits for its terminal to
     close, so it can't attach to the one that is going. One that couldn't
     close stays stuck on the daemon: every start restarts until one has
     replaced it, so it is never the terminal a start is handed back. */
  const start = useCallback(async (restart = false) => {
    const n = ++attempt.current;
    setPhase({ kind: "starting" });
    setLink(null);
    setAction(null);
    finished.current = false;
    pending.current = null;
    const cleanup = closing.current;
    const unclosed = (await cleanup) !== null;
    if (n !== attempt.current) return;
    let started: ActionTerminal;
    try {
      started = await client.providerLogin(provider, undefined, restart || unclosed, loginContext);
    } catch (e) {
      if (n === attempt.current) setPhase({ kind: "failed", terminal: null, message: message(e) });
      return;
    }
    // The daemon has ended the terminal that couldn't be closed, whichever start this was.
    if (closing.current === cleanup) closing.current = Promise.resolve(null);
    if (n !== attempt.current) return;
    setPhase({ kind: "waiting", terminal: started.terminal });
    setAction({ terminal: started.terminal, at: { server: started.server ?? null, target: started.target } });
  }, [client, provider, loginContext]);
  useEffect(
    () => () => {
      attempt.current++;
    },
    [],
  );

  // agy stays at its chat prompt after browser sign-in. Completion is the
  // provider's confirmed credentials, even when its terminal never exits.
  useEffect(() => {
    if (!action) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await ending.current(action.terminal, false);
      if (live && !finished.current) timer = setTimeout(poll, CHECK_MS);
    };
    timer = setTimeout(poll, CHECK_MS);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [action]);

  // The CLI's words, read off its screen as it changes: a link, a question, a code to paste, the end. Read
  // once the pty view can (the shell may say so only after the sign-in started), until it ends or is cancelled.
  useEffect(() => {
    if (!action || !open) return;
    const { terminal, at } = action;
    const reading = (reader.current = readPane(
      open,
      at,
      (text) => {
        const found = readLink(text);
        if (found) setLink((had) => keptLink(had, found));
        const prompt = readPrompt(text);
        const failure = readFailure(text);
        setPhase((p) => {
          if (p.kind !== "waiting" && p.kind !== "choose" && p.kind !== "code") return p;
          // Said, with Try again; the checks go on, so a sign-in the browser finishes after all still counts.
          if (failure) return { kind: "failed", terminal, message: failure };
          if (!prompt) return p.kind === "waiting" ? p : { kind: "waiting", terminal };
          return prompt.kind === "choose" ? { kind: "choose", terminal, question: prompt.question, choices: prompt.choices } : { kind: "code", terminal, prompt: prompt.prompt };
        });
      },
      (error) => {
        if (error === undefined) void ending.current(terminal);
        else if (!finished.current) setPhase({ kind: "failed", terminal, message: error });
      },
    ));
    if (fitted.current) reading.resize(fitted.current.cols, fitted.current.rows);
    return () => {
      reading.close();
      if (reader.current === reading) reader.current = null;
    };
  }, [action, open]);

  /* The details' size while they show the CLI's pane (tmux then wraps it at
     their width), null once they don't: the reader reads at that size, else its own. */
  const fitReader = useCallback((size: { cols: number; rows: number } | null) => {
    fitted.current = size;
    const { cols, rows } = size ?? READ_SIZE;
    reader.current?.resize(cols, rows);
  }, []);

  // The pty view can't run here: said, not waited on (a tmux installed since, and Check, is the way on).
  useEffect(() => {
    if (!action || !unusable || finished.current) return;
    setPhase((p) => (p.kind === "waiting" ? { kind: "failed", terminal: action.terminal, message: `The app can't follow ${name}'s sign-in: ${unusable}` } : p));
  }, [action, unusable, name]);

  /* One line the person gave (a choice, a code), typed into the CLI; true once it's sent. */
  const answer = async (terminal: string, line: string) => {
    const n = attempt.current;
    try {
      await client.terminalInput(terminal, line);
      if (n !== attempt.current) return false;
      setPhase({ kind: "waiting", terminal });
      return true;
    } catch (e) {
      if (n === attempt.current) setPhase({ kind: "failed", terminal, message: message(e) });
      return false;
    }
  };
  /* Cancel or Close: the CLI's terminal goes too, so a sign-in stuck where it
     failed isn't the one the next press finds still running and attaches to.
     One that couldn't be closed (not merely gone already) is said, with Try again. */
  const cancel = () => {
    const n = ++attempt.current;
    if (action) {
      const closed = (closing.current = client.closeTerminal(action.terminal).then(
        () => null,
        (e) => (e instanceof DaemonError && e.status === 404 ? null : `Couldn't end ${name}'s sign-in: ${message(e)}`),
      ));
      void closed.then((why) => {
        if (why && n === attempt.current) setPhase({ kind: "failed", terminal: null, message: why });
      });
    }
    setAction(null);
    finished.current = true;
    setPhase({ kind: "idle" });
  };
  return { client, name, phase, link, pane: action?.at ?? null, views, start, answer, cancel, fitReader };
}

/* The CLI's own pane, in a terminal of the card's size; the reader reads at
   that size while it shows (tmux wraps the pane at it), at its own once it goes. */
function SignInDetails({ at, views, fitReader }: { at: PtyViewTarget; views: PtyViews; fitReader(size: { cols: number; rows: number } | null): void }) {
  useEffect(() => () => fitReader(null), [fitReader]);
  return <PtyTerminal at={at} open={views.open} copy={views.copy} paste={views.paste} onSize={(cols, rows) => fitReader({ cols, rows })} />;
}

export function ProviderSignIn({ provider, loginContext, check, onDone, autoStart = false, small = false, onActive, details: offerDetails = true }: ProviderSignInProps) {
  const { name, phase, link, pane, views, start, answer: send, cancel, fitReader } = useProviderSignIn({ provider, loginContext, check, onDone, onActive, details: offerDetails });
  const [details, setDetails] = useState(false);
  const [code, setCode] = useState("");
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  const begin = (restart = false) => {
    setDetails(false);
    setCopied(false);
    setCopyError(false);
    return start(restart);
  };
  const copyLink = async () => {
    if (!link || !views) return;
    try {
      await views.copy(link);
      setCopied(true);
      setCopyError(false);
    } catch {
      setCopyError(true);
    }
  };
  const answer = async (terminal: string, line: string) => {
    if (await send(terminal, line)) setCode("");
  };

  useEffect(() => {
    if (autoStart && phase.kind === "idle") void begin();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoStart]);

  if (phase.kind === "idle") {
    return (
      <button type="button" className={small ? `${primary} px-s1 py-0.5 text-xs` : primary} onClick={() => void begin()}>
        Sign in to {name}
      </button>
    );
  }
  const terminal = "terminal" in phase ? phase.terminal : null;
  const busy = phase.kind === "starting" || phase.kind === "waiting" || phase.kind === "checking";
  return (
    <div className="flex w-full flex-col gap-s1 rounded-md border border-line bg-canvas-2 px-s2 py-s1 text-sm" role="status" aria-label={`Sign in to ${name}`} data-testid="sign-in-flow" data-phase={phase.kind}>
      <div className="flex flex-wrap items-center gap-s1">
        {busy && <span className="size-3 shrink-0 animate-spin rounded-full border-2 border-branch border-t-transparent" aria-hidden />}
        {phase.kind === "starting" && <span>Starting {name}'s sign-in…</span>}
        {phase.kind === "waiting" && <span>Finish signing in to {name} in your browser…</span>}
        {phase.kind === "checking" && <span>Confirming your sign-in…</span>}
        {phase.kind === "done" && <span className="font-medium text-pass">Signed in to {name} ✓</span>}
        {phase.kind === "failed" && <span className="font-medium text-sap">Couldn't sign in to {name}</span>}
        {phase.kind === "choose" && <span>{phase.question}</span>}
        {phase.kind === "code" && <span>{phase.prompt}</span>}
        <span className="flex-1" />
        {(phase.kind === "waiting" || phase.kind === "choose" || phase.kind === "code") && link && (
          <>
            <button type="button" className={quiet} onClick={() => void openExternal(link).catch(() => undefined)}>
              Open browser ↗
            </button>
            {views && <button type="button" className={quiet} onClick={() => void copyLink()}>
              {copied ? "Link copied" : "Copy sign-in link"}
            </button>}
          </>
        )}
        {offerDetails && terminal && pane && views && phase.kind !== "done" && (
          <button type="button" className={`${quiet} text-xs`} aria-expanded={details} onClick={() => setDetails((d) => !d)}>
            {details ? "Hide details" : "Show details"}
          </button>
        )}
        {phase.kind === "done" || phase.kind === "failed" ? (
          <button type="button" className={quiet} onClick={cancel}>
            Close
          </button>
        ) : (
          <button type="button" className={quiet} onClick={cancel}>
            Cancel
          </button>
        )}
      </div>
      {(phase.kind === "starting" || phase.kind === "waiting") && (
        <p className="text-xs text-ink-2">Follow the steps in your browser, then come back here. This updates automatically when you're signed in. Your password stays with {name}.</p>
      )}
      {copyError && link && (
        <label className="flex flex-col gap-1 text-xs text-ink-2">
          Couldn't copy the sign-in link. Select it below and copy it into your browser.
          <input className={`${field} w-full`} aria-label="Sign-in link" readOnly value={link} onFocus={(e) => e.currentTarget.select()} />
        </label>
      )}
      {phase.kind === "choose" && (
        <div className="flex flex-wrap gap-s1" role="group" aria-label="Sign-in method">
          {phase.choices.map((c) => (
            <button key={c.n} type="button" className={button} onClick={() => void answer(phase.terminal, c.n)}>
              {c.label}
            </button>
          ))}
        </div>
      )}
      {phase.kind === "code" && (
        <form
          className="flex gap-s1"
          onSubmit={(e) => {
            e.preventDefault();
            if (code.trim()) void answer(phase.terminal, code.trim());
          }}
        >
          <input className={`${field} w-full font-mono`} aria-label="Code from the browser" placeholder="Paste the code from the browser" value={code} autoFocus onChange={(e) => setCode(e.target.value)} />
          <button type="submit" className={primary} disabled={!code.trim()}>
            Continue
          </button>
        </form>
      )}
      {phase.kind === "failed" && (
        <p className={alert} role="alert">
          {phase.message}{" "}
          <button type="button" className={primary} onClick={() => void begin(true)}>
            Try again
          </button>
        </p>
      )}
      {offerDetails && details && terminal && pane && views && (
        <div className="flex h-64 flex-col overflow-hidden rounded-md border border-shell-line bg-shell-bg" data-testid="sign-in-details">
          <div className="border-b border-shell-line px-s2 py-1 font-mono text-xs text-shell-ink">{name} sign-in</div>
          <SignInDetails key={terminal} at={pane} views={views} fitReader={fitReader} />
        </div>
      )}
    </div>
  );
}
