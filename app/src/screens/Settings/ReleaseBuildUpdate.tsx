/* Staff-only "Install latest release-branch build" (owner decision): the
   newest build the release/desktop pipeline published, for staff to try
   before it ships (the shell's app_release_build_install and app_restart,
   api/tauri.ts). A row of App update's section (SettingsScreen), which shows
   it only when GET /debug says this account is staff and the shell gives the
   updater (none in a browser tab); anyone else sees nothing. The shell picks
   the feed itself, asks the backend afresh that the account is staff and
   checks that updates are on before reading it and again before installing,
   checks the bundle's signature, and runs one install at a time; here is
   only the button, how the install goes, the build installed, and Restart. */

import { useState } from "react";
import type { ReleaseBuildUpdater, ReleaseProgress } from "../../api/tauri";
import { plain } from "../../api/wording";
import { alert, button, primary } from "../../components/ui";

type Step =
  | { step: "idle" }
  | { step: "working"; version: string | null; progress: ReleaseProgress | null }
  | { step: "latest" }
  | { step: "installed" | "restarting"; version: string };

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

/* What the shell said last, as the row's help. */
function working(version: string | null, progress: ReleaseProgress | null): string {
  if (!progress) return "Checking the release branch's latest build…";
  if (progress.kind === "installing") return `Installing brindle ${version}…`;
  if (progress.kind === "found") return `Found brindle ${progress.version}.`;
  const mb = (progress.downloaded / 1e6).toFixed(1);
  return progress.total ? `Downloading brindle ${version}: ${mb} of ${(progress.total / 1e6).toFixed(1)} MB` : `Downloading brindle ${version}: ${mb} MB`;
}

/* A release-branch build's version carries `+release.<run>.<sha>` build
   metadata (the pipeline's contract, src-tauri/src/updates.rs). */
const RELEASE_BUILD = /\+release\.\d+\.[0-9a-f]{12}$/;

/* The rows: the latest release-branch build, and, when this build is one
   (`version`, About's), Return to stable. */
export function ReleaseBuildUpdate({ updater, version }: { updater: ReleaseBuildUpdater; version?: string | null }) {
  return (
    <>
      <BuildRow
        testId="release-build-row"
        label="Latest release-branch build"
        idle="Installs the newest build of the release branch, before it ships."
        latest="This is the release branch's latest build already."
        action="Install"
        updater={updater}
        run={updater.install}
      />
      {version && RELEASE_BUILD.test(version) && (
        <BuildRow
          testId="return-to-stable-row"
          label="Return to stable"
          idle="Goes back to the current stable release, even at this version."
          latest="The stable release is not newer than this build."
          action="Return to stable"
          check={updater.checkStable}
          confirm={(v) => `Installs brindle ${v} stable; restart to finish.`}
          updater={updater}
          run={updater.returnToStable}
        />
      )}
    </>
  );
}

/* `check`/`confirm`: ask the shell first which version this would install,
   show it, and install only that one after Confirm (`run` gets it back). */
function BuildRow({
  testId,
  label,
  idle,
  latest,
  action,
  check,
  confirm,
  updater,
  run,
}: {
  testId: string;
  label: string;
  idle: string;
  latest: string;
  action: string;
  check?: () => Promise<string | null>;
  confirm?: (version: string) => string;
  updater: ReleaseBuildUpdater;
  run: (progress: (p: ReleaseProgress) => void, target: string) => Promise<string | null>;
}) {
  const [state, setState] = useState<Step>({ step: "idle" });
  const [asking, setAsking] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ask = async () => {
    setChecking(true);
    setError(null);
    try {
      const target = await check!();
      if (target) {
        setState({ step: "idle" });
        setAsking(target);
      } else setState({ step: "latest" });
    } catch (e) {
      setError(message(e));
    } finally {
      setChecking(false);
    }
  };
  const install = async (target = "") => {
    setAsking(null);
    setState({ step: "working", version: null, progress: null });
    setError(null);
    try {
      const version = await run((progress) =>
        setState((s) => ({
          step: "working",
          version: progress.kind === "found" ? progress.version : s.step === "working" ? s.version : null,
          progress,
        })),
        target,
      );
      setState(version ? { step: "installed", version } : { step: "latest" });
    } catch (e) {
      setError(message(e));
      setState({ step: "idle" });
    }
  };
  const restart = async (version: string) => {
    setState({ step: "restarting", version });
    try {
      await updater.restart();
    } catch (e) {
      setError(message(e));
      setState({ step: "installed", version });
    }
  };
  const help =
    state.step === "working"
      ? working(state.version, state.progress)
      : state.step === "installed" || state.step === "restarting"
        ? `brindle ${state.version} is installed. Restart to use it.`
        : state.step === "latest"
          ? latest
          : asking && confirm
            ? confirm(asking)
            : idle;
  const downloading = state.step === "working" && state.progress?.kind === "downloading" ? state.progress : null;
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-s3 gap-y-1 px-s2 py-s1" data-testid={testId}>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-x-s1 text-sm font-medium">{label}</div>
        <p className="text-xs text-ink-3">{help}</p>
        {downloading && (
          <progress className="w-full" aria-label="Download" value={downloading.downloaded} max={downloading.total ?? undefined} />
        )}
      </div>
      <div className="flex min-w-0 flex-wrap items-center justify-end gap-s1">
        {state.step === "installed" || state.step === "restarting" ? (
          <button type="button" className={primary} disabled={state.step === "restarting"} onClick={() => void restart(state.version)}>
            Restart
          </button>
        ) : asking ? (
          <>
            <button type="button" className={primary} onClick={() => void install(asking)}>
              Confirm
            </button>
            <button type="button" className={button} onClick={() => setAsking(null)}>
              Cancel
            </button>
          </>
        ) : (
          <button type="button" className={button} disabled={state.step === "working" || checking} onClick={() => (check ? void ask() : void install())}>
            {state.step === "working" ? "Installing…" : checking ? "Checking…" : action}
          </button>
        )}
      </div>
      {error && (
        <p role="alert" className={`${alert} col-span-2`}>
          Could not install: {error}
        </p>
      )}
    </div>
  );
}
