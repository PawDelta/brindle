/* The first launch's engine step, full window as the rest of it
   (screens/Onboarding): the app found no engine and couldn't start one.
   One button installs it (the shell runs the equivalent of brindle's
   install script on a pty of its own: api/setup.ts, adding uv, brindle,
   and tmux where Homebrew can), its lines streaming into the calm progress
   card, never a terminal; then the engine starts and the first launch goes
   on to its sign-ins and providers (App's gate). A failure, of the install
   or the start, says what happened inline with Retry. Nothing to type. */

import { useCallback, useEffect, useRef, useState } from "react";
import { connectOnceStarted, tooOldToStart } from "../../api/engine";
import type { Detection, Setup } from "../../api/setup";
import { plain } from "../../api/wording";
import { ProgressCard } from "../../components/ProgressCard";
import { plainText } from "../../components/ProviderSignIn";
import { alert, mainTone, quiet } from "../../components/ui";
import { Frame, Spinner } from "../Onboarding/Frame";

export interface FirstRunProps {
  setup: Setup;
  /* Why there's no engine (the shell's reason: brindle isn't installed ...). */
  reason: string;
  /* The engine runs: on into the app (its gate goes on with the first launch). */
  onReady(): void;
}

type Phase =
  | { kind: "idle" }
  | { kind: "installing" }
  | { kind: "install-failed"; message: string }
  | { kind: "starting" }
  | { kind: "start-failed"; message: string };

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));
const big = `inline-flex items-center gap-1 rounded-md border px-s2 py-s1 ${mainTone}`;

/* What the install will add, from what the shell found. */
export function installSteps(d: Detection | null): string {
  const has = (name: string) => !!d?.tools.find((t) => t.name === name)?.path;
  const parts = [has("uv") ? null : "uv", "brindle", d && !has("tmux") && d.homebrew ? "tmux (with Homebrew)" : null].filter(Boolean);
  return parts.join(", ");
}

/* Why there's no engine, as the person reads it: the shell's reason, or for
   an engine too old to start (whose reason names the command that would
   upgrade it) what to do without one. */
export const OUT_OF_DATE = "This brindle engine is out of date: update the app.";
export const readableReason = (reason: string): string => (tooOldToStart(reason) ? OUT_OF_DATE : reason);

/* The pane's bytes as lines, the last few kept. */
const linesOf = (text: string) =>
  text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .slice(-50);

export function FirstRun({ setup, reason, onReady }: FirstRunProps) {
  const [detection, setDetection] = useState<Detection | null>(null);
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [lines, setLines] = useState<string[]>([]);
  /* Installs started; only the last one's pane is heard. */
  const run = useRef(0);
  const pane = useRef<{ close(): void } | null>(null);

  const detect = useCallback(() => setup.detect().then(setDetection, () => setDetection(null)), [setup]);
  useEffect(() => {
    void detect();
    return () => pane.current?.close();
  }, [detect]);

  /* Start the engine (a first start takes a while, and that isn't a failure); on into the app. */
  const start = useCallback(async () => {
    setPhase({ kind: "starting" });
    try {
      await connectOnceStarted(setup.connect);
    } catch (e) {
      void detect();
      setPhase({ kind: "start-failed", message: message(e) });
      return;
    }
    onReady();
  }, [setup, detect, onReady]);

  const install = async () => {
    const which = ++run.current;
    pane.current?.close();
    setLines([]);
    setPhase({ kind: "installing" });
    try {
      await setup.install();
    } catch (e) {
      setPhase({ kind: "install-failed", message: `The install didn't start: ${message(e)}` });
      return;
    }
    let text = "";
    /* A run ends once: a later exit (a pane attached again) changes nothing. */
    let ended = false;
    pane.current = setup.attach({
      onError(why) {
        if (which === run.current) setPhase({ kind: "install-failed", message: plain(why) });
      },
      onData(bytes) {
        if (which !== run.current) return;
        text = (text + plainText(bytes)).slice(-16000);
        setLines(linesOf(text));
      },
      onExit(code) {
        if (which !== run.current || ended) return;
        ended = true;
        if (code === 0) void start();
        else setPhase({ kind: "install-failed", message: `It stopped (exit ${code}); its last lines are above.` });
      },
    });
  };

  const busy = phase.kind === "installing" || phase.kind === "starting";
  const ran = run.current > 0;
  return (
    <Frame
      step="engine"
      narrow
      kicker="Starting"
      title="Set up brindle's engine"
      lede={`This app runs on brindle's engine, which isn't running on this Mac yet. Installing it adds ${installSteps(detection)}, takes about a minute and needs the network.`}
    >
      <p className="text-xs text-ink-3" data-testid="first-run-reason">
        {readableReason(reason)}
      </p>
      {ran && (
        <ProgressCard
          label="Installing brindle's engine"
          testId="engine-install"
          title={<span className="font-semibold">brindle's engine</span>}
          status={phase.kind === "install-failed" ? "failed" : phase.kind === "installing" ? "running" : "done"}
          lines={lines}
          failure={{ headline: "Install failed.", message: phase.kind === "install-failed" ? phase.message : "" }}
          onRetry={() => void install()}
        />
      )}
      <div className="mt-s1 flex flex-wrap items-center gap-s2">
        {phase.kind === "starting" ? (
          <span className="inline-flex items-center gap-s1 text-sm" role="status">
            <Spinner />
            Starting brindle…
          </span>
        ) : (
          !ran && (
            <button type="button" className={big} disabled={busy} onClick={() => void install()}>
              Install brindle's engine
            </button>
          )
        )}
        {!busy && (
          <button type="button" className={quiet} onClick={() => void start()}>
            {phase.kind === "start-failed" ? "Retry" : "Try starting it again"}
          </button>
        )}
      </div>
      {phase.kind === "start-failed" && (
        <p className={alert} role="alert">
          The engine didn't start: {phase.message}
        </p>
      )}
    </Frame>
  );
}
