/* Settings › Library: the person's own switches for their Library of approved plans
   (GET /analysis-docs/switches), each set with PUT /analysis-docs/switches.
   The `store` switch is the Library location: This Mac (off) or brindle servers (on);
   changing it moves the plans there, and the engine turns the switches that need the
   servers on (servers) or off (This Mac) in the same update; the reply carries them all. A switch their organisation enforces is shown
   locked, with the org's value, and can't be changed here. Without the analysis_docs
   plan, or an engine without the route, the section isn't shown. */

import { useEffect, useState } from "react";
import type { DaemonClient, DocSwitch, DocSwitchName } from "../../api/client";
import { plain } from "../../api/wording";
import { alert, card, h2, notice } from "../../components/ui";

export type DocSwitchesClient = Pick<DaemonClient, "docSwitches" | "setDocSwitch">;

const ABOUT: Record<DocSwitchName, { name: string; help: string }> = {
  store: { name: "Library location", help: "Where your approved plans are kept." },
  share: { name: "Sharing", help: "Let your docs be shared with your teams and organisation." },
  assist: { name: "Briefs", help: "Give new work a brief from the docs that match it." },
  prompt_analysis: { name: "Prompt analysis", help: "Record which docs your prompts matched (never the prompt itself)." },
  import_upload: { name: "Imported folders", help: "Let folders you add to your library be uploaded." },
};

/* The two Library locations: the store switch off and on. */
const LOCATIONS: { on: boolean; label: string; help: string }[] = [
  { on: false, label: "This Mac", help: "Private to you. When you share a plan, only that plan goes to brindle servers." },
  { on: true, label: "brindle servers", help: "Keep your Library on brindle servers. Turns on Sharing, Briefs, Imported folders and Prompt analysis, unless set by your organisation." },
];

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));
const where = (on: boolean) => (on ? "brindle servers" : "This Mac");

export function DocSwitches({ client }: { client: DocSwitchesClient }) {
  const [switches, setSwitches] = useState<DocSwitch[] | null>(null);
  const [busy, setBusy] = useState<DocSwitchName | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [move, setMove] = useState<{ on: boolean; moved: number; left: number } | null>(null);

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

  const store = switches.find(s => s.name === "store");
  const onServers = store?.on === true;
  const serverSwitches = switches.filter(s => s.name !== "store");
  const enabled = serverSwitches.filter(s => s.on).map(s => ABOUT[s.name].name);
  const enabledNames = enabled.length > 1 ? `${enabled.slice(0, -1).join(", ")} and ${enabled.at(-1)}` : enabled[0];

  const set = async (name: DocSwitchName, on: boolean) => {
    setBusy(name);
    setError(null);
    try {
      const got = await client.setDocSwitch(name, on);
      setSwitches(got.switches);
      if (name === "store") setMove(previous => ({
        on,
        moved: (previous?.on === on && previous.left > 0 ? previous.moved : 0) + (got.moved ?? 0),
        left: got.left ?? 0,
      }));
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(null);
    }
  };

  const renderSwitch = (s: DocSwitch) => {
    const about = ABOUT[s.name] ?? { name: s.name, help: "" };
    const locked = `Set by your organisation: ${s.org === "enforced_on" ? "on" : "off"}.`;
    const help = s.locked ? `${locked} ${s.reason ?? ""}`.trim()
      : !onServers ? s.name === "share"
        ? "Needs brindle servers. On this Mac you can still share one plan at a time from its Share panel."
        : "Needs brindle servers. Move your plans to brindle servers to turn this on."
      : about.help;
    return (
      <div key={s.name} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-s3 px-s2 py-s1" data-testid={`doc-switch-${s.name}`} data-state={s.locked ? "locked" : !onServers ? "unavailable" : "available"}>
        <div className="min-w-0">
          <div className="text-sm font-medium">{about.name}</div>
          <p id={`doc-switch-${s.name}-help`} className="text-xs text-ink-3">{help}</p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={s.on}
          aria-label={about.name}
          aria-describedby={`doc-switch-${s.name}-help`}
          disabled={s.locked || !onServers || busy !== null}
          className={`relative h-5 w-9 shrink-0 rounded-full border transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${s.on ? "border-branch bg-branch" : "border-line-strong bg-surface-2"}`}
          onClick={() => void set(s.name, !s.on)}
        >
          <span aria-hidden className={`absolute top-0.5 size-3.5 rounded-full transition-[left] ${s.on ? "left-[18px] bg-on-branch" : "left-0.5 bg-ink-3"}`} />
        </button>
      </div>
    );
  };

  return (
    <section id="doc-switches" aria-labelledby="doc-switches-title" className="flex scroll-mt-s2 flex-col gap-s1" data-testid="doc-switches">
      <header className="flex flex-wrap items-baseline gap-x-s2">
        <h2 id="doc-switches-title" className={h2}>Library</h2>
        <span className="text-xs text-ink-3">Where your approved plans live and what brindle does with them.</span>
      </header>
      {store && (
        <div className={`${card} px-s2 py-s1`} data-testid="doc-switch-store">
          <div className="text-sm font-medium">Keep my plans</div>
          {store.locked && <p className="text-xs text-ink-3">{`Set by your organisation: ${where(store.org === "enforced_on")}. ${store.reason ?? ""}`.trim()}</p>}
          <div role="radiogroup" aria-label={ABOUT.store.name} className="mt-s1 flex flex-col gap-s1">
            {LOCATIONS.map(l => (
              <button
                key={l.label}
                type="button"
                role="radio"
                aria-label={l.label}
                aria-checked={store.on === l.on}
                aria-describedby={`doc-location-${l.on}-help`}
                disabled={store.locked || busy !== null}
                className="flex items-start gap-s1 rounded py-s1 text-left disabled:cursor-not-allowed disabled:opacity-50"
                onClick={() => store.on !== l.on && void set("store", l.on)}
              >
                <span aria-hidden className={`mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full border ${store.on === l.on ? "border-branch" : "border-line-strong"}`}>
                  {store.on === l.on && <span className="size-2 rounded-full bg-branch" />}
                </span>
                <span>
                  <span className="block text-sm">On {l.label === "This Mac" ? "this Mac" : l.label}</span>
                  <span id={`doc-location-${l.on}-help`} className="block text-xs text-ink-3">{l.help}</span>
                </span>
              </button>
            ))}
          </div>
        </div>
      )}
      {move?.on && onServers && (
        <div role="status" className={`${notice} flex flex-wrap items-center justify-between gap-s1`}>
          <span><strong>{move.moved} {move.moved === 1 ? "plan" : "plans"} moved to brindle servers.</strong>{enabledNames && ` ${enabledNames} ${enabled.length === 1 ? "is" : "are"} on.`}</span>
          <button type="button" className="text-xs underline disabled:opacity-50" disabled={store?.locked || busy !== null} onClick={() => void set("store", false)}>Move back</button>
        </div>
      )}
      <div role="group" aria-labelledby="doc-server-switches-title" className="mt-s2">
        <h3 id="doc-server-switches-title" className="px-s2 py-s1 text-xs font-medium uppercase text-ink-3">With plans on brindle servers</h3>
        <div className={`${card} flex flex-col divide-y divide-line`}>
          {serverSwitches.map(renderSwitch)}
        </div>
      </div>
      {move && move.left > 0 && (
        <div role="alert" className={alert}>
          {move.left} {move.left === 1 ? "plan wasn't" : "plans weren't"} moved yet.{" "}
          <button type="button" className="underline disabled:opacity-50" disabled={store?.locked || busy !== null} onClick={() => void set("store", move.on)}>Retry</button>
        </div>
      )}
      {error && (
        <p role="alert" className={alert}>
          {error}
        </p>
      )}
    </section>
  );
}
