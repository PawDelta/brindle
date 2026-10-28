/* The first launch (design/connect/, owner-approved, with the owner's final
   provider model): its own full-window screens, no sidebar, from a clean
   Mac to a first project. Starting brindle (Starting.tsx, while the app
   asks the engine) → Sign in to brindle (one button; the browser does it:
   POST /account/login) → Connect your providers (a card each: a CLI found
   on this Mac is used as-is and says where it was found; Connect on a
   missing one runs its maker's official installer, as-is, into its normal
   place) → the installs, one calm progress card each with the installer's
   own lines (POST /providers/{p}/install, a job; a failure is a strip on
   its card with Retry and Skip for now) → each provider's own sign-in in
   the browser (components/ProviderSignIn's workings; brindle never sees the
   credentials). Both sign-ins, while the browser is waited on and after a
   failure, offer Try again and Skip for now → You're ready → Pick a project (open a folder or clone a
   repository: components/ProjectPicker) → the normal app, on New session
   in that project.

   App shows it until one provider is connected and signed in (providers.ts
   `onboarded`); later launches go straight to the app. Each CLI keeps
   itself up to date: nothing here offers an update. Every step is a
   button: nothing to type but a URL to clone, no popup. */

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import type { DaemonClient, PermissionPreset, ProviderInstalled } from "../../api/client";
import type { OpenUrl, PickFolder } from "../../api/open";
import { plain } from "../../api/wording";
import { ProgressCard } from "../../components/ProgressCard";
import { ProjectPicker } from "../../components/ProjectPicker";
import { useProviderSignIn } from "../../components/ProviderSignIn";
import { alert, button, card, field, mono, primary, quiet } from "../../components/ui";
import type { Route } from "../../routes";
import { useSessions } from "../../store/sessions";
import { useJob } from "../Branch/useJob";
import { Frame, Spinner } from "./Frame";
import { connected, factsOf, markOnboarded, names, onboarded, signedIn, unsigned } from "../../api/providers";
import { useInstalls, type Install } from "../../api/providersInstall";
import { Starting } from "./Starting";

export interface OnboardingProps {
  client: DaemonClient;
  /* What the gate read: the flow starts where the person is. */
  providers: ProviderInstalled[];
  /* Opens a page in the system browser. */
  openUrl: OpenUrl;
  /* The native folder picker (Pick a project's Open a folder; none in a browser tab). */
  pickFolder?: PickFolder;
  /* Over: into the app, on New session in the project picked. */
  onDone(route?: Route): void;
}

type Step = "loading" | "signin" | "connect" | "install" | "provider-signin" | "ready" | "permissions" | "project";

const big = `${primary} px-s2 py-s1 text-[15px]`;
const link = "text-branch underline-offset-2 hover:underline";
const fine = "text-xs text-ink-3";

/* The provider's mark: its initial on its own colour (tokens only). */
const MARKS: Record<string, string> = { claude: "bg-sap text-on-sap", codex: "bg-ink text-canvas", opencode: "bg-ink-3 text-canvas", antigravity: "bg-branch text-on-branch" };
function Mark({ provider, name, small = false }: { provider: string; name: string; small?: boolean }) {
  return (
    <span aria-hidden className={`inline-grid shrink-0 place-items-center rounded-md font-semibold ${small ? "size-4 text-[9px]" : "size-7 text-xs"} ${MARKS[provider] ?? "bg-branch text-on-branch"}`}>
      {small ? "" : name[0]}
    </span>
  );
}

/* A tick in a circle: done (pass), or not yet. */
function Tick({ on = true }: { on?: boolean }) {
  return (
    <span aria-hidden className={`inline-grid size-4 shrink-0 place-items-center rounded-full border text-[9px] font-bold ${on ? "border-pass bg-pass text-canvas" : "border-line-strong text-transparent"}`}>
      ✓
    </span>
  );
}

/* A browser sign-in's way out, while it waits and after it fails: start it
   afresh, or go on and sign in later (Account, Settings › Providers). */
function Escape({ onRetry, onSkip }: { onRetry(): void; onSkip(): void }) {
  return (
    <>
      <button type="button" className={button} data-testid="onb-signin-retry" onClick={onRetry}>
        Try again
      </button>
      <button type="button" className={quiet} data-testid="onb-signin-skip" onClick={onSkip}>
        Skip for now
      </button>
    </>
  );
}

/* ---- Step 1: sign in to brindle, in the browser. ---- */

function BrindleSignIn({ client, openUrl, onSignedIn, onSkip }: { client: DaemonClient; openUrl: OpenUrl; onSignedIn(): void; onSkip(): void }) {
  const { job, busy, start, clear } = useJob();
  const opened = useRef<string | null>(null);
  const url = job?.status === "running" ? (job.progress.find((line) => /^https?:\/\//.test(line)) ?? null) : null;
  // The authorize URL arrives on the bus: open it once.
  useEffect(() => {
    if (!url || opened.current === url) return;
    opened.current = url;
    void openUrl(url).catch(() => undefined);
  }, [url, openUrl]);
  useEffect(() => {
    if (job?.status === "done") onSignedIn();
  }, [job?.status, onSignedIn]);

  // Try again restarts: a sign-in still running is ended and a fresh one opens in the browser.
  const begin = (restart = false) => {
    opened.current = null;
    void start("Sign in to brindle", () => client.accountLogin(false, restart));
  };
  const skip = () => {
    clear();
    onSkip();
  };
  const failed = job?.status === "failed";
  return (
    <Frame step="signin" narrow kicker="Step 1 of 3" title="Sign in to brindle" lede="Your account keeps your sessions, repos and usage together on every Mac you use.">
      <div className="mt-s1 flex flex-wrap items-center gap-s2">
        {busy ? (
          <>
            <span className="inline-flex items-center gap-s1 text-sm" role="status">
              <Spinner />
              Waiting for the browser…
            </span>
            {url && (
              <button type="button" className={`${quiet} ${link}`} onClick={() => void openUrl(url).catch(() => undefined)}>
                Open the tab again
              </button>
            )}
            <Escape onRetry={() => begin(true)} onSkip={skip} />
          </>
        ) : failed ? (
          <Escape onRetry={() => begin(true)} onSkip={skip} />
        ) : (
          <button type="button" className={big} onClick={() => begin()}>
            Sign in with your browser
          </button>
        )}
      </div>
      {failed && (
        <p className={alert} role="alert">
          The sign-in didn't finish: {plain(job?.error ?? "")}
        </p>
      )}
      <p className={fine}>
        {busy ? "A tab opened in your browser. Finish there; this screen moves on by itself." : "A tab opens in your browser. Come back here when it's done; nothing to type here."}
        {(busy || failed) && " You can also sign in later, from Account."}
      </p>
    </Frame>
  );
}

/* ---- Step 2: connect your providers. ---- */

function ProviderCard({ row, picked, installed, onPick, onUndo }: { row: ProviderInstalled; picked: boolean; installed: boolean; onPick(): void; onUndo(): void }) {
  const f = factsOf(row);
  const found = connected(row);
  return (
    <section className={`${card} flex flex-col gap-s1 p-s2 ${picked ? "border-branch ring-1 ring-branch" : found ? "border-pass" : ""}`} aria-label={f.name} data-testid={`onb-card-${row.provider}`} data-picked={picked} data-found={found}>
      <div className="flex items-center gap-s1">
        <Mark provider={row.provider} name={f.name} />
        <div className="leading-tight">
          <p className="font-semibold">{f.name}</p>
          <p className={fine}>by {f.maker}</p>
        </div>
      </div>
      {f.blurb && <p className="text-sm text-ink-2">{f.blurb}</p>}
      {f.license.label && <p className={fine}>{f.license.label}</p>}
      <div className="mt-auto pt-s1">
        {found ? (
          <div className="flex items-start gap-s1 text-sm" data-testid={`onb-found-${row.provider}`}>
            <Tick />
            <span className="min-w-0">
              {installed ? (
                <>Installed by {f.maker}'s installer</>
              ) : (
                <>
                  Found at <span className={`${mono} break-all`}>{row.path}</span>
                </>
              )}
              {row.version && <span className="text-ink-3"> · {row.version}</span>}
            </span>
          </div>
        ) : picked ? (
          <div className="flex items-center gap-s1 text-sm" data-testid={`onb-chosen-${row.provider}`}>
            <Tick />
            <span>Runs {f.maker}'s installer</span>
            <span className="flex-1" />
            <button type="button" className={`${quiet} ${link}`} onClick={onUndo}>
              Undo
            </button>
          </div>
        ) : (
          <button type="button" className={button} onClick={onPick}>
            Connect
          </button>
        )}
      </div>
    </section>
  );
}

/* ---- The installs: the calm progress card, or one line each. ---- */

function Line({ row, ok, note }: { row: ProviderInstalled; ok: boolean; note: ReactNode }) {
  const f = factsOf(row);
  return (
    <div className={`${card} flex flex-wrap items-center gap-s1 px-s2 py-s1 ${ok ? "border-pass" : ""}`} data-testid={`onb-install-${row.provider}`} data-state={ok ? "done" : "waiting"}>
      <Tick on={ok} />
      <Mark provider={row.provider} name={f.name} small />
      <span className="font-semibold">{f.name}</span>
      {row.version && <span className={`${mono} text-ink-3`}>{row.version}</span>}
      <span className="flex-1" />
      <span className={`${fine} text-right`}>{note}</span>
    </div>
  );
}

function InstallCard({ row, install, openUrl, onRetry, onSkip }: { row: ProviderInstalled; install: Install; openUrl: OpenUrl; onRetry(): void; onSkip(): void }) {
  const f = factsOf(row);
  return (
    <ProgressCard
      label={`Installing ${f.name}`}
      testId={`onb-install-${row.provider}`}
      title={
        <>
          <Mark provider={row.provider} name={f.name} />
          <span className="font-semibold">{f.name}</span>
          <span className="text-sm text-ink-3">{f.maker}'s installer</span>
        </>
      }
      status={install.status === "starting" ? "running" : install.status}
      lines={install.lines}
      failure={{ headline: "Install failed.", message: plain(install.error ?? "") }}
      onRetry={onRetry}
      other={{ label: "Skip for now", onClick: onSkip }}
    >
      <dl className="grid grid-cols-[96px_1fr] gap-x-s2 gap-y-1 text-sm">
        {f.installer.label && (
          <>
            <dt className="text-ink-3">From</dt>
            <dd>
              <button type="button" className={link} onClick={() => void openUrl(f.installer.url).catch(() => undefined)}>
                {f.installer.label}
              </button>{" "}
              <span className="text-ink-2">· {f.maker}'s official installer, run as-is</span>
            </dd>
          </>
        )}
        <dt className="text-ink-3">Into</dt>
        <dd className="text-ink-2">Where {f.maker}'s installer always puts it; {f.name} keeps itself up to date</dd>
        {f.license.label && (
          <>
            <dt className="text-ink-3">License</dt>
            <dd>
              <button type="button" className={link} onClick={() => void openUrl(f.license.url).catch(() => undefined)}>
                {f.license.label}
              </button>{" "}
              <span className="text-ink-2">· installing accepts these with {f.maker}; brindle isn't a party to them</span>
            </dd>
          </>
        )}
      </dl>
    </ProgressCard>
  );
}

/* ---- Step 3: each provider's own sign-in, in the browser. ---- */

/* Signed in, `check` has read the providers again and the flow moves on by itself. */
function ProviderSignInStep({ row, index, of, rest, already, check, openUrl, onLater, problem }: {
  /* The providers couldn't be read again (the check's own read): said in place of "still isn't signed in". */
  problem?: ReactNode;
  row: ProviderInstalled;
  index: number;
  of: number;
  /* Who comes next, and who is signed in already. */
  rest: string[];
  already: string[];
  check(): Promise<boolean>;
  openUrl: OpenUrl;
  onLater(): void;
}) {
  const f = factsOf(row);
  const { phase, link: tab, start, answer } = useProviderSignIn({ provider: row.provider, check, details: false });
  const [code, setCode] = useState("");
  const waiting = phase.kind === "starting" || phase.kind === "waiting" || phase.kind === "checking" || phase.kind === "done";
  const terminal = "terminal" in phase ? phase.terminal : null;
  // Under way (but not just signed in), or failed: Try again or Skip for now, never a dead end.
  const stuck = phase.kind !== "idle" && phase.kind !== "done";
  return (
    <Frame
      step="provider-signin"
      narrow
      kicker={`Step 3 of 3${of > 1 ? ` · ${index + 1} of ${of}` : ""}`}
      title={`Sign in to ${f.signin}`}
      lede={`${f.name}${row.version ? ` ${row.version}` : ""} is ready. It signs you in itself, with ${f.maker}, in your browser, using ${f.account}.`}
    >
      <div className="mt-s1 flex flex-wrap items-center gap-s2" data-testid="onb-provider-signin" data-phase={phase.kind}>
        {waiting ? (
          <>
            <span className="inline-flex items-center gap-s1 text-sm" role="status">
              <Spinner />
              Waiting for the browser…
            </span>
            {tab && (
              <button type="button" className={`${quiet} ${link}`} onClick={() => void openUrl(tab).catch(() => undefined)}>
                Open the tab again
              </button>
            )}
          </>
        ) : phase.kind === "choose" ? (
          <div className="flex flex-col gap-s1">
            <p className="text-sm">{phase.question}</p>
            <div className="flex flex-wrap gap-s1" role="group" aria-label="Sign-in method">
              {phase.choices.map((c) => (
                <button key={c.n} type="button" className={button} onClick={() => void answer(phase.terminal, c.n)}>
                  {c.label}
                </button>
              ))}
            </div>
          </div>
        ) : phase.kind === "code" ? (
          <form
            className="flex w-full gap-s1"
            onSubmit={(e) => {
              e.preventDefault();
              if (terminal && code.trim()) void answer(terminal, code.trim()).then((sent) => sent && setCode(""));
            }}
          >
            <input className={`${field} w-full font-mono`} aria-label="Code from the browser" placeholder={phase.prompt} value={code} autoFocus onChange={(e) => setCode(e.target.value)} />
            <button type="submit" className={primary} disabled={!code.trim()}>
              Continue
            </button>
          </form>
        ) : phase.kind === "failed" ? null : (
          <>
            <button type="button" className={big} onClick={() => void start()}>
              Sign in with your browser
            </button>
            <button type="button" className={quiet} onClick={onLater}>
              Do this later
            </button>
          </>
        )}
        {stuck && (
          <div className="flex flex-wrap items-center gap-s2">
            <Escape
              onRetry={() => {
                setCode("");
                void start(true);
              }}
              onSkip={onLater}
            />
          </div>
        )}
      </div>
      {problem ||
        (phase.kind === "failed" && (
          <p className={alert} role="alert">
            {plain(phase.message)}
          </p>
        ))}
      <p className={fine}>
        brindle never sees the credentials; {f.name} keeps them where it always does.
        {stuck && " You can also sign in later, from Settings › Providers."}
        {rest.length > 0 && ` ${names(rest)} ${rest.length > 1 ? "come" : "comes"} next.`}
        {already.length > 0 && ` ${names(already)} ${already.length > 1 ? "are" : "is"} signed in already.`}
      </p>
    </Frame>
  );
}

/* ---- You're ready. ---- */

function Ready({ rows, installed, onSignIn, onNext }: { rows: ProviderInstalled[]; installed: string[]; onSignIn(provider: string): void; onNext(): void }) {
  const have = rows.filter(connected);
  const others = rows.length - have.length;
  const count = ["", "one", "two", "three"][others] ?? String(others);
  return (
    <Frame step="ready" narrow kicker="Ready" title="You're ready">
      <div className={`${card} flex flex-col divide-y divide-line border-pass`} data-testid="onb-ready">
        {have.map((r) => {
          const f = factsOf(r);
          const signed = signedIn(r);
          return (
            <div key={r.provider} className="flex flex-wrap items-center gap-s1 px-s2 py-s1" data-testid={`onb-ready-${r.provider}`}>
              <Tick on={signed} />
              <Mark provider={r.provider} name={f.name} small />
              <span className="font-semibold">{f.name}</span>
              {r.version && <span className={`${mono} text-ink-3`}>{r.version}</span>}
              <span className="flex-1" />
              <span className={fine}>
                {signed ? (
                  `signed in · ${installed.includes(r.provider) ? `installed by ${f.maker}'s installer` : "your own copy"}, it keeps itself updated`
                ) : (
                  <>
                    {r.signed_in === null ? "sign-in not confirmed" : "not signed in yet"} ·{" "}
                    <button type="button" className={link} onClick={() => onSignIn(r.provider)}>
                      Sign in
                    </button>
                  </>
                )}
              </span>
            </div>
          );
        })}
      </div>
      <div>
        <button type="button" className={big} onClick={onNext}>
          Continue
        </button>
      </div>
      <p className={fine}>
        From now on your providers live in Settings › Providers: where each one is and its sign-in
        {others > 0 ? `, and the other ${count} whenever you want ${others === 1 ? "it" : "them"}` : ""}.
      </p>
    </Frame>
  );
}

/* ---- Permissions: how much the agents do on their own (design/setup-presets.html). ---- */

interface PresetCopy {
  preset: PermissionPreset;
  name: string;
  who: string;
  runs: string;
  asks: string;
  blocked: string;
  /* Of 4: how many bars of the interruptions meter are lit. */
  bars: number;
  often: string;
  note: string;
}

const PRESETS: PresetCopy[] = [
  {
    preset: "autonomous",
    name: "Autonomous",
    who: "Agents work start to finish without stopping. Best for most people: you review the result, not every step.",
    runs: "Anything no rule covers: reading, writing and editing files, running commands, fetching, pushing to work branches",
    asks: "Pushing into your default branch (remembered answers can last), pushing into release/* (every time)",
    blocked: "Force pushes, deleting the root or home folder, reading credential files (~/.ssh, ~/.aws, .env)",
    bars: 1,
    often: "rarely",
    note: "With Autonomous, brindle also groups similar requests (for example every push into your default branch) so one answer covers them all.",
  },
  {
    preset: "permissive",
    name: "Permissive",
    who: "Files in the worktree and everyday tools run freely; other requests ask, and you can remember the answer.",
    runs: "Reading tracked files, reading, writing and editing files in the worktree, everyday dev tools (npm, uv, pytest, docker, gh...), checks, read-only commands",
    asks: "Other commands, files outside the worktree, pushes, web and connected tools; pushing into release/* every time",
    blocked: "Force pushes, deleting the root or home folder, reading credential files",
    bars: 2,
    often: "sometimes",
    note: "Permissive asks for other commands, files outside the worktree and pushes. Remembered answers stop repeat questions.",
  },
  {
    preset: "standard",
    name: "Standard",
    who: "Agents read and run everyday tools freely but ask before writing or editing files directly. Good while you're getting to know brindle.",
    runs: "Reading tracked files, everyday dev tools (npm, uv, pytest, docker, gh...; these can change files), checks",
    asks: "Direct file writes and edits, other commands, pushes, web and tool access; pushing into release/* every time",
    blocked: "Force pushes, deleting the root or home folder, reading credential files",
    bars: 3,
    often: "often",
    note: "Standard pauses agents often. If they stall, switch to Autonomous in Settings → Permissions, or allow the noisiest rules there.",
  },
  {
    preset: "secure",
    name: "Secure",
    who: "For sensitive code: almost everything asks, approvals last at most 15 minutes, and anything no rule covers is denied.",
    runs: "Reading tracked files",
    asks: "Every write, edit, command, fetch and push (remembered for at most 15 minutes); pushing into release/* every time",
    blocked: "Force pushes, deleting the root or home folder, credential files, anything no rule covers",
    bars: 4,
    often: "constantly",
    note: "Secure pauses agents on almost every step and denies anything no rule covers. Expect to approve requests often.",
  },
];

function PresetCard({ copy, on, disabled, buttonRef, onPick, onKey }: { copy: PresetCopy; on: boolean; disabled: boolean; buttonRef(el: HTMLButtonElement | null): void; onPick(): void; onKey(e: KeyboardEvent<HTMLButtonElement>): void }) {
  return (
    <button
      ref={buttonRef}
      type="button"
      role="radio"
      aria-checked={on}
      disabled={disabled}
      tabIndex={on ? 0 : -1}
      data-testid={`onb-preset-${copy.preset}`}
      className={`${card} flex flex-col items-stretch gap-s1 p-s2 text-left aria-checked:border-branch aria-checked:bg-branch-soft`}
      onClick={onPick}
      onKeyDown={onKey}
    >
      <span className="flex items-center gap-s1">
        <span aria-hidden className={`size-[18px] shrink-0 rounded-full border-2 ${on ? "border-branch bg-branch shadow-[inset_0_0_0_4px_var(--surface)]" : "border-line-strong"}`} />
        <span className="text-base font-semibold">{copy.name}</span>
        {copy.preset === "autonomous" && <span className="ml-auto rounded-sm bg-branch-soft px-1.5 py-0.5 text-[10px] font-semibold tracking-wide text-branch">RECOMMENDED</span>}
      </span>
      <span className="text-[13px] text-ink-2">{copy.who}</span>
      <span className="grid gap-1 border-t border-line pt-s1 text-[13px]">
        {(
          [
            ["Runs on its own", copy.runs, "text-pass"],
            ["Asks you", copy.asks, "text-branch"],
            ["Blocked", copy.blocked, "text-sap"],
          ] as const
        ).map(([k, v, tone]) => (
          <span key={k} className="grid grid-cols-[96px_1fr] gap-s1">
            <span className={`pt-0.5 text-[11px] font-semibold uppercase tracking-wide ${tone}`}>{k}</span>
            <span className="text-ink-2">{v}</span>
          </span>
        ))}
      </span>
      <span className="flex items-center gap-s1 text-xs text-ink-3">
        Interruptions
        <span aria-hidden className="flex gap-[3px]">
          {[1, 2, 3, 4].map((n) => (
            <i key={n} className={`h-1.5 w-3.5 rounded-sm ${n <= copy.bars ? "bg-sap" : "bg-line-strong"}`} />
          ))}
        </span>
        {copy.often}
      </span>
    </button>
  );
}

/* Continue applies the choice (PUT /permissions/preset); Skip for now leaves the engine's
   own default, Autonomous. */
function Permissions({ client, onNext }: { client: DaemonClient; onNext(): void }) {
  const [choice, setChoice] = useState<PermissionPreset>("autonomous");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  const copy = PRESETS.find((p) => p.preset === choice) ?? PRESETS[0];

  const move = (from: number, step: number) => {
    const next = PRESETS[(from + step + PRESETS.length) % PRESETS.length];
    setChoice(next.preset);
    refs.current[next.preset]?.focus();
  };
  const apply = async () => {
    setBusy(true);
    setError(null);
    try {
      await client.setPermissionPreset(choice);
      onNext();
    } catch (e) {
      setError(plain(e instanceof Error ? e.message : String(e)));
      setBusy(false);
    }
  };

  return (
    <Frame step="permissions" kicker="Permissions" title="How much should your agents do on their own?" lede="Agents stop and wait whenever they need your permission. Pick how often that happens. You can change this, and edit or remove any rule it adds, in Settings → Permissions.">
      <div className="mt-s1 grid gap-s2 sm:grid-cols-2" role="radiogroup" aria-label="Permission preset">
        {PRESETS.map((p, i) => (
          <PresetCard
            key={p.preset}
            copy={p}
            on={p.preset === choice}
            disabled={busy}
            buttonRef={(el) => void (refs.current[p.preset] = el)}
            onPick={() => setChoice(p.preset)}
            onKey={(e) => {
              const d = ({ ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 } as Record<string, number>)[e.key];
              if (!d) return;
              e.preventDefault();
              move(i, d);
            }}
          />
        ))}
      </div>
      <p className="flex items-start gap-s1 rounded-md border border-line bg-canvas-2 px-s2 py-s1 text-[13px] text-ink-2" data-testid="onb-preset-note">
        <span aria-hidden className="text-branch">◇</span>
        {copy.note}
      </p>
      {error && (
        <p className={alert} role="alert">
          Couldn't apply {copy.name}: {error}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-s2">
        <span className={`${fine} min-w-0 flex-1`}>Nothing is locked in. Every rule a preset adds can be edited or removed later.</span>
        <button type="button" className={quiet} disabled={busy} onClick={onNext}>
          Skip for now
        </button>
        <button type="button" className={big} disabled={busy} onClick={() => void apply()}>
          Continue with {copy.name}
        </button>
      </div>
    </Frame>
  );
}

/* ---- The flow. ---- */

export function Onboarding({ client, providers, openUrl, pickFolder, onDone }: OnboardingProps) {
  const [step, setStep] = useState<Step>("loading");
  const [rows, setRows] = useState<ProviderInstalled[]>(providers);
  /* The missing CLIs picked to install. */
  const [picks, setPicks] = useState<string[]>([]);
  /* Sign-ins put off with "Do this later" in this flow. */
  const [later, setLater] = useState<string[]>([]);

  /* The providers read again failed: the step stays where it is and says so, with Retry. */
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const reload = useCallback(async () => {
    try {
      const got = await client.providersInstalled();
      setRows(got);
      setRefreshError(null);
      return got;
    } catch (e) {
      setRefreshError(plain(e instanceof Error ? e.message : String(e)));
      throw e;
    }
  }, [client]);
  const refreshProblem = refreshError && (
    <div className={`${alert} flex flex-wrap items-center gap-s2`} role="alert" data-testid="onb-refresh-failed">
      <span className="min-w-0 flex-1">Couldn't read your providers again: {refreshError}</span>
      <button type="button" className={button} onClick={() => void reload().catch(() => undefined)}>
        Retry
      </button>
    </div>
  );

  // After brindle's sign-in, the cards: a CLI already on this Mac shows there as found.
  const afterSignIn = useCallback(() => setStep("connect"), []);

  // Where the person is: signed in to brindle already, or not (an engine without accounts goes on).
  useEffect(() => {
    let live = true;
    client.account().then(
      (a) => live && (a.signed_in ? afterSignIn() : setStep("signin")),
      () => live && afterSignIn(),
    );
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client]);

  // The cards read what's on this Mac afresh.
  useEffect(() => {
    if (step === "connect") void reload().catch(() => undefined);
  }, [step, reload]);

  const order = rows.map((r) => r.provider);
  const queue = order.filter((p) => picks.includes(p));
  const { installs, start, forget } = useInstalls(client, () => void reload().catch(() => undefined));
  const installed = Object.entries(installs)
    .filter(([, i]) => i.status === "done")
    .map(([p]) => p);

  // The installs run one after another: the next starts once nothing runs or waits on a retry.
  useEffect(() => {
    if (step !== "install") return;
    if (queue.some((p) => installs[p] && installs[p].status !== "done")) return;
    const next = queue.find((p) => !installs[p]);
    if (next) void start(next);
  }, [step, queue, installs, start]);

  const skip = (provider: string) => {
    forget(provider);
    const rest = picks.filter((p) => p !== provider);
    setPicks(rest);
    if (rest.length === 0 && !rows.some(connected)) setStep("connect");
  };

  // A provider confirmed connected and signed in: the first launch is done for good, whatever happens after.
  const finished = onboarded(rows);
  useEffect(() => {
    if (finished) markOnboarded();
  }, [finished]);

  const pending = useMemo(() => rows.filter((r) => unsigned(r) && !later.includes(r.provider)), [rows, later]);
  /* The sign-ins this round began with ("1 of 2"), kept as each one finishes. */
  const [round, setRound] = useState<string[]>([]);
  useEffect(() => {
    if (step !== "provider-signin") return;
    if (pending.length === 0) setStep("ready");
    else setRound((r) => (pending.every((p) => r.includes(p.provider)) ? r : pending.map((p) => p.provider)));
  }, [step, pending]);

  const checkSignedIn = useCallback(
    (provider: string) => async () => {
      const got = await reload();
      return got.find((r) => r.provider === provider)?.signed_in === true;
    },
    [reload],
  );

  const label = (p: string) => factsOf(rows.find((r) => r.provider === p) ?? { provider: p, name: p }).name;

  // The project picked: the app's repo list reads it before the app opens on it (no event says a repo was added).
  const { reload: reloadRepos } = useSessions();
  const picked = async (repo: string) => {
    await reloadRepos().catch(() => undefined);
    onDone({ screen: "new", repo });
  };

  switch (step) {
    case "loading":
      // The gate's Starting screen, a moment longer.
      return <Starting />;
    case "signin":
      return <BrindleSignIn client={client} openUrl={openUrl} onSignedIn={afterSignIn} onSkip={afterSignIn} />;
    case "connect": {
      const found = rows.filter(connected);
      return (
        <Frame
          step="connect"
          kicker="Step 2 of 3"
          title="Connect your providers"
          lede="Pick the coding agents you'll run, one or more. brindle runs each maker's own installer, as it is, and uses the CLI where the installer puts it; each keeps itself up to date. One you already have, brindle simply uses."
        >
          <div className="mt-s1 grid gap-s2 sm:grid-cols-2">
            {rows.map((r) => (
              <ProviderCard
                key={r.provider}
                row={r}
                picked={picks.includes(r.provider)}
                installed={installed.includes(r.provider)}
                onPick={() => setPicks((all) => [...all, r.provider])}
                onUndo={() => setPicks((all) => all.filter((p) => p !== r.provider))}
              />
            ))}
          </div>
          {refreshProblem}
          <div className="mt-s1 flex flex-wrap items-center gap-s2">
            <button type="button" className={big} disabled={(queue.length === 0 && found.length === 0) || !!refreshError} onClick={() => setStep("install")}>
              {queue.length ? `Connect ${names(queue.map(label))}` : found.length ? `Continue with ${names(found.map((r) => factsOf(r).name))}` : "Connect"}
            </button>
            <p className={`${fine} min-w-0 flex-1`}>
              {queue.length
                ? "Each runs its maker's official installer, unchanged. Installing accepts that maker's own terms with the maker; brindle isn't a party to them."
                : found.length
                  ? "brindle uses what it found where it is, and doesn't touch it. You can add the others any time in Settings › Providers."
                  : "Pick at least one to go on. You can add the others any time in Settings › Providers."}
            </p>
          </div>
        </Frame>
      );
    }
    case "install": {
      const shown = rows.filter((r) => queue.includes(r.provider) || connected(r));
      const done = queue.every((p) => installs[p]?.status === "done");
      const list = names(shown.map((r) => factsOf(r).name));
      return (
        <Frame
          step="install"
          kicker="Step 2 of 3"
          title={done ? `${list} ${shown.length > 1 ? "are" : "is"} connected` : `Connecting ${list}`}
          lede={done ? "Each sits where its installer put it, or stays yours where it is. Next, each one's own sign-in." : "This runs on its own: each maker's installer, as it is."}
        >
          {shown.map((r) => {
            const f = factsOf(r);
            const i = installs[r.provider];
            if (i?.status === "done") return <Line key={r.provider} row={r} ok note={<>Installed by {f.maker}'s installer{r.path && <> at <span className={mono}>{r.path}</span></>}.</>} />;
            if (!queue.includes(r.provider))
              return (
                <Line
                  key={r.provider}
                  row={r}
                  ok
                  note={
                    <>
                      Found at <span className={mono}>{r.path}</span>; it updates itself, brindle doesn't touch it.
                    </>
                  }
                />
              );
            if (!i) return <Line key={r.provider} row={r} ok={false} note="Waiting · installs next" />;
            return <InstallCard key={r.provider} row={r} install={i} openUrl={openUrl} onRetry={() => void start(r.provider)} onSkip={() => skip(r.provider)} />;
          })}
          {refreshProblem}
          {/* On only with the providers as the engine reads them now: a failed read stays here, with Retry. */}
          {done && !refreshError && (
            <div>
              <button
                type="button"
                className={big}
                onClick={() =>
                  void reload().then(
                    () => setStep("provider-signin"),
                    () => undefined,
                  )
                }
              >
                Continue
              </button>
            </div>
          )}
        </Frame>
      );
    }
    case "provider-signin": {
      const row = pending[0];
      if (!row) return null;
      const already = rows.filter(signedIn).map((r) => factsOf(r).name);
      const at = round.indexOf(row.provider);
      return (
        <ProviderSignInStep
          key={row.provider}
          row={row}
          index={Math.max(at, 0)}
          of={Math.max(round.length, 1)}
          rest={pending.slice(1).map((r) => factsOf(r).name)}
          already={already}
          check={checkSignedIn(row.provider)}
          openUrl={openUrl}
          onLater={() => setLater((l) => [...l, row.provider])}
          problem={refreshProblem}
        />
      );
    }
    case "ready":
      return (
        <Ready
          rows={rows}
          installed={installed}
          onSignIn={(p) => {
            setLater((l) => l.filter((x) => x !== p));
            setStep("provider-signin");
          }}
          onNext={() => setStep("permissions")}
        />
      );
    case "permissions":
      return <Permissions client={client} onNext={() => setStep("project")} />;
    case "project":
      return (
        <Frame step="project" kicker="Ready" title="Pick a project" lede="The folder brindle works in: one on this Mac, or a repository to clone. Your first session starts there.">
          <ProjectPicker client={client} pickFolder={pickFolder} onPicked={(repo) => void picked(repo.repo_root)} />
        </Frame>
      );
  }
}
