import type { EngineStatus } from "../api/engine";
import { BELOW_API_MAJOR, MIN_API } from "../api/engine";

const SUPPORTED = `${MIN_API.join(".")} up to ${BELOW_API_MAJOR}.0`;
export const UPGRADE_COMMAND = "uv tool upgrade brindle";

const STRIP = "flex items-center gap-s2 border-b border-line px-s3 py-s1 text-sm";
const ACTION = "shrink-0 rounded-md border border-current px-s1 py-0.5 text-xs font-semibold disabled:cursor-default disabled:opacity-60";

/* What the banner's upgrade is doing, when the app runs it (components/EngineUpgrade). */
export type UpgradeProgress = "idle" | "running" | "failed";

/* The strip above the app when its engine isn't one it can simply use: too
   old (upgrade it, run by the engine itself in the background when the app
   can ask it to, with the button saying so while it runs or after it
   failed; the daemon exits after an upgrade and the next check starts the
   new one), newer than the app (it still runs: update the app),
   unreachable, or refusing this app. Nothing while connecting or when all
   is well. `onRetry` checks again. */
export function EngineBanner({ status, onRetry, onUpgrade, upgrading = "idle" }: { status: EngineStatus; onRetry?: () => void; onUpgrade?: () => void; upgrading?: UpgradeProgress }) {
  if (status.state === "connecting" || status.state === "ok") return null;
  const retry = onRetry && (
    <button type="button" className={`${ACTION} ml-auto`} onClick={onRetry}>
      Check again
    </button>
  );
  if (status.state === "update-app") {
    const { engine_version, api_version } = status.meta;
    return (
      <div role="status" className={`${STRIP} bg-branch-soft text-ink`} data-testid="engine-banner">
        <strong className="shrink-0 text-branch">Update available</strong>
        <span className="text-ink-2">
          brindle {engine_version} (API {api_version}) is newer than this app knows. It still works; update the app
          for everything it offers.
        </span>
      </div>
    );
  }
  let text;
  let action = null;
  if (status.state === "upgrade-engine") {
    const { engine_version, api_version } = status.meta;
    text = (
      <span>
        brindle {engine_version} (API {api_version || "unknown"}) is older than this app supports (API {SUPPORTED}).
        {onUpgrade ? " Upgrade the engine; the app reconnects to the new one." : <> Upgrade the engine: <code className="rounded-sm bg-canvas-2 px-1 font-mono text-ink">{UPGRADE_COMMAND}</code></>}
      </span>
    );
    action = onUpgrade && (
      <button type="button" className={`${ACTION} ml-auto bg-sap text-on-sap`} disabled={upgrading === "running"} onClick={onUpgrade} data-testid="banner-upgrade">
        {upgrading === "running" ? "Upgrading…" : upgrading === "failed" ? "Upgrade failed · Retry" : "Upgrade the engine"}
      </button>
    );
  } else if (status.state === "refused") {
    text = (
      <span>
        The brindle engine refused this app ({status.message}). Open the app yourself, not from an agent's terminal.
      </span>
    );
  } else {
    text = <span>Can't reach the brindle engine: {status.message}</span>;
  }
  return (
    <div role="alert" className={`${STRIP} bg-sap-soft text-sap`} data-testid="engine-banner">
      {text}
      {action}
      {action ? onRetry && <button type="button" className={ACTION} onClick={onRetry}>Check again</button> : retry}
    </div>
  );
}
