/* Settings › Analysis docs: the person's own switches for their doc library
   (GET /analysis-docs/switches), each set with PUT /analysis-docs/switches.
   A switch their organisation enforces is shown locked, with the org's value,
   and can't be changed here. Without the analysis_docs plan, or an engine
   without the route, the section isn't shown. */

import { useEffect, useState } from "react";
import type { DaemonClient, DocSwitch, DocSwitchName } from "../../api/client";
import { plain } from "../../api/wording";
import { alert, card, h2 } from "../../components/ui";

export type DocSwitchesClient = Pick<DaemonClient, "docSwitches" | "setDocSwitch">;

const ABOUT: Record<DocSwitchName, { name: string; help: string }> = {
  store: { name: "Store docs", help: "Keep each branch's analysis doc in your library once it merges." },
  share: { name: "Sharing", help: "Let your docs be shared with your teams and organisation." },
  assist: { name: "Briefs", help: "Give new work a brief from the docs that match it." },
  prompt_analysis: { name: "Prompt analysis", help: "Record which docs your prompts matched (never the prompt itself)." },
  import_upload: { name: "Imported folders", help: "Let folders you add to your library be uploaded." },
};

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

export function DocSwitches({ client }: { client: DocSwitchesClient }) {
  const [switches, setSwitches] = useState<DocSwitch[] | null>(null);
  const [busy, setBusy] = useState<DocSwitchName | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    client.docSwitches().then(
      (s) => live && setSwitches(s),
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [client]);

  if (!switches) return null;

  const set = async (name: DocSwitchName, on: boolean) => {
    setBusy(name);
    setError(null);
    try {
      setSwitches(await client.setDocSwitch(name, on));
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section id="doc-switches" aria-labelledby="doc-switches-title" className="flex scroll-mt-s2 flex-col gap-s1" data-testid="doc-switches">
      <header className="flex flex-wrap items-baseline gap-x-s2">
        <h2 id="doc-switches-title" className={h2}>
          Analysis docs
        </h2>
        <span className="text-xs text-ink-3">What brindle Pro does with your analysis docs.</span>
      </header>
      <div className={`${card} flex flex-col divide-y divide-line`}>
        {switches.map((s) => {
          const about = ABOUT[s.name] ?? { name: s.name, help: "" };
          return (
            <div key={s.name} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-s3 px-s2 py-s1" data-testid={`doc-switch-${s.name}`}>
              <div className="min-w-0">
                <div className="text-sm font-medium">{about.name}</div>
                <p className="text-xs text-ink-3">
                  {s.locked ? `Set by your organisation: ${s.org === "enforced_on" ? "on" : "off"}.` : about.help}
                </p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={s.on}
                aria-label={about.name}
                disabled={s.locked || busy !== null}
                className={`relative h-5 w-9 shrink-0 rounded-full border transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${s.on ? "border-branch bg-branch" : "border-line-strong bg-surface-2"}`}
                onClick={() => void set(s.name, !s.on)}
              >
                <span aria-hidden className={`absolute top-0.5 size-3.5 rounded-full transition-[left] ${s.on ? "left-[18px] bg-on-branch" : "left-0.5 bg-ink-3"}`} />
              </button>
            </div>
          );
        })}
      </div>
      {error && (
        <p role="alert" className={alert}>
          {error}
        </p>
      )}
    </section>
  );
}
