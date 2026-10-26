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

export function ReleaseBuildUpdate({ updater }: { updater: ReleaseBuildUpdater }) {
  const [state, setState] = useState<Step>({ step: "idle" });
  const [error, setError] = useState<string | null>(null);
  const install = async () => {
    setState({ step: "working", version: null, progress: null });
    setError(null);
    try {
      const version = await updater.install((progress) =>
        setState((s) => ({
          step: "working",
          version: progress.kind === "found" ? progress.version : s.step === "working" ? s.version : null,
          progress,
        })),
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
          ? "This is the release branch's latest build already."
          : "Installs the newest build of the release branch, before it ships.";
  const downloading = state.step === "working" && state.progress?.kind === "downloading" ? state.progress : null;
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-s3 gap-y-1 px-s2 py-s1" data-testid="release-build-row">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-x-s1 text-sm font-medium">Latest release-branch build</div>
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
        ) : (
          <button type="button" className={button} disabled={state.step === "working"} onClick={() => void install()}>
            {state.step === "working" ? "Installing…" : "Install"}
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
