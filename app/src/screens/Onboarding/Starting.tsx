/* The first thing the app shows, the whole window: the engine starts
   inside the app (screens/FirstRun's Launch waits on it here), and the app
   asks it what is set up (App's first-launch gate). One quiet line and a
   slim bar; nothing to do. When the asking fails, what went wrong and
   Retry in the line's place. */

import Logo from "../../components/Logo";
import { alert, button } from "../../components/ui";

export function Starting({ error, onRetry }: { error?: string | null; onRetry?(): void }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-s2 bg-canvas px-s3 text-ink" data-testid="starting">
      <Logo className="text-2xl" />
      {error ? (
        <div className={`${alert} flex max-w-md flex-wrap items-center gap-s2`} role="alert" data-testid="starting-failed">
          <span className="min-w-0 flex-1">Couldn't ask the engine what's set up: {error}</span>
          {onRetry && (
            <button type="button" className={button} onClick={onRetry}>
              Retry
            </button>
          )}
        </div>
      ) : (
        <>
          <div className="onb-bar h-1 w-48 overflow-hidden rounded-full bg-line" aria-hidden>
            <i />
          </div>
          <p className="text-sm text-ink-2" role="status">
            Starting brindle…
          </p>
        </>
      )}
      <p className="max-w-sm text-center text-xs text-ink-3">The engine runs inside the app, from its own folder. Nothing is added to your PATH or anywhere else on your Mac.</p>
    </div>
  );
}
