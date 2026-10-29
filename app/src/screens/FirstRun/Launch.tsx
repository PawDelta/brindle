/* Before the app: find the daemon (the shell starts it when none runs). An
   engine still starting (its first start can take a while) is waited for,
   behind "Starting brindle…". When
   it can't be found or started, the first launch's engine step instead
   (FirstRun), until it is; one too old to run says it's out of date there,
   and its install brings it up to date. */

import { useEffect, useState, type ReactNode } from "react";
import { connectOnceStarted, startingLine, type StartStep } from "../../api/engine";
import { usePtyViews } from "../../api/ptyView";
import type { Setup } from "../../api/setup";
import { Starting } from "../Onboarding/Starting";
import { FirstRun } from "./FirstRun";

type State = { kind: "connecting" } | { kind: "setup"; reason: string } | { kind: "ready" };

/* How often "Starting brindle…" asks the shell what the start is doing. */
const STEP_EVERY_MS = 500;

export function Launch({ setup, children }: { setup: Setup; /* The app, once the daemon runs. */ children: () => ReactNode }) {
  const [state, setState] = useState<State>({ kind: "connecting" });
  useEffect(() => {
    let live = true;
    connectOnceStarted(setup.connect, () => live).then(
      () => live && setState({ kind: "ready" }),
      (e) => live && setState({ kind: "setup", reason: e instanceof Error ? e.message : String(e) }),
    );
    return () => {
      live = false;
    };
  }, [setup]);

  // While it connects, what the shell's start is doing (stopping the engine from before an update, starting this one).
  const connecting = state.kind === "connecting";
  const [step, setStep] = useState<StartStep | null>(null);
  useEffect(() => {
    const ask = setup.startStep;
    if (!connecting || !ask) return;
    let live = true;
    const read = () => void ask().then((s) => live && setStep(s), () => {});
    read();
    const every = setInterval(read, STEP_EVERY_MS);
    return () => {
      live = false;
      clearInterval(every);
    };
  }, [connecting, setup]);

  // Ready (a first run may just have installed tmux): the pane view asks the shell again.
  const views = usePtyViews();
  const ready = state.kind === "ready";
  const recheck = views?.recheck;
  useEffect(() => {
    if (ready) recheck?.();
  }, [ready, recheck]);

  if (state.kind === "ready") return children();
  // An engine too old to start says so in the engine step's own words, without the command a banner would name.
  if (state.kind === "setup") return <FirstRun setup={setup} reason={state.reason} onReady={() => setState({ kind: "ready" })} />;
  // The first launch's own first screen (screens/Onboarding): the app's gate shows it on while it asks the engine.
  return <Starting line={startingLine(step)} />;
}
