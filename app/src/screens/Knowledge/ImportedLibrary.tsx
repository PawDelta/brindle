/* "Add to library" for an imported folder (brindle Pro). Folders stay local
   until the person confirms the exact list of files that would be uploaded;
   anything that looks like a secret is skipped and said so. */
import { useState } from "react";
import type { DaemonClient, KbLibraryPlan, KbSource } from "../../api/client";
import { plain } from "../../api/wording";
import { alert, button, main, notice, quiet } from "../../components/ui";

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));
const count = (n: number) => (n === 1 ? "1 file" : `${n} files`);

export function ImportedLibrary({ client, repo, source, disabled, onChanged }: { client: DaemonClient; repo: string; source: KbSource; disabled: boolean; onChanged(): void }) {
  const [plan, setPlan] = useState<KbLibraryPlan | null>(null);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (what: () => Promise<unknown>, after?: () => void) => {
    setWorking(true);
    setError(null);
    try {
      await what();
      after?.();
    } catch (e) {
      setError(message(e));
    } finally {
      onChanged();
      setWorking(false);
    }
  };

  return (
    <div className="flex basis-full flex-col gap-1">
      {source.library_state === "in_library" || source.library_state === "removing" ? (
        <div className="flex items-center gap-s1 text-xs text-ink-3">
          <span>
            {source.library_state === "removing"
              ? source.library_removing
                ? `Removal unfinished: ${count(source.library_removing)} still in your library.`
                : "Removal unfinished."
              : source.library_pending
                ? `In your library, but ${count(source.library_pending)} did not upload yet: re-index to try again.`
                : "In your library."}
          </span>
          <button
            type="button"
            className={quiet}
            disabled={disabled || working}
            onClick={() =>
              void run(async () => {
                const { left } = await client.removeKbFromLibrary(repo, source.id);
                if (left > 0) setError(`${count(left)} could not be deleted from your library yet. Use Finish removing to try again.`);
              })
            }
          >
            {source.library_state === "removing" ? "Finish removing" : "Remove from library"}
          </button>
        </div>
      ) : (
        /* Local, or an add that did not finish: the engine answers busy while one is still running. */
        !plan && (
          <div className="flex items-center gap-1">
            <button type="button" className={quiet} disabled={disabled || working} onClick={() => void run(async () => setPlan(await client.kbLibraryPlan(repo, source.id)))}>
              {source.library_state === "adding" ? "Resume adding" : "Add to library"}
            </button>
            {source.library_state === "adding" && (
              <button type="button" className={quiet} disabled={disabled || working} onClick={() => void run(() => client.removeKbFromLibrary(repo, source.id))}>
                Remove
              </button>
            )}
          </div>
        )
      )}
      {plan && (
        <section className="flex flex-col gap-1 rounded border border-line px-s2 py-s1" aria-label="Files to upload" data-testid="library-plan">
          <p className="text-sm">{plan.files.length === 0 ? "Nothing in this folder can be uploaded." : `These ${count(plan.files.length)} will be uploaded to your library:`}</p>
          <ul className="text-xs text-ink-2" aria-label="Will upload">
            {plan.files.map((f) => (
              <li key={f.name}>{f.name}</li>
            ))}
          </ul>
          {plan.skipped.length > 0 && (
            <>
              <p className="text-sm">Skipped, not uploaded:</p>
              <ul className="text-xs text-ink-3" aria-label="Skipped">
                {plan.skipped.map((s) => (
                  <li key={s.name}>
                    {s.name}: {s.reason}
                  </li>
                ))}
              </ul>
            </>
          )}
          {!plan.allowed && <p className={notice}>Uploading imported folders is off for you, so nothing can be sent.</p>}
          <div className="flex gap-1">
            <button
              type="button"
              className={main}
              disabled={working || !plan.allowed || plan.files.length === 0}
              onClick={() =>
                void run(async () => {
                  const got = await client.addKbToLibrary(repo, source.id, plan.token);
                  if (got.changed) {
                    setPlan(got);
                    setError("The folder changed since this list was made. Check the new list before uploading.");
                  } else setPlan(null);
                })
              }
            >
              Upload {count(plan.files.length)}
            </button>
            <button type="button" className={button} disabled={working} onClick={() => setPlan(null)}>
              Cancel
            </button>
          </div>
        </section>
      )}
      {error && (
        <p role="alert" className={alert}>
          {error}
        </p>
      )}
    </div>
  );
}
