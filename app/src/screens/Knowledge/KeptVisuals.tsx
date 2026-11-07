/* Knowledge › Kept visuals: every visual kept from a session's reader (Keep), newest first, and
   the same reader opened read-only. A kept copy lives on its own (.brindle/kept/visuals/), so it
   reads after its session is gone. Not per repo: the list says which repo each came from. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { KeptVisualDTO } from "../../api/client";
import { plain } from "../../api/wording";
import { alert, button, card } from "../../components/ui";
import { formatAge } from "../../store/overview";
import type { KeptVisualsClient } from "../Session/Visuals/client";
import { ReaderChrome } from "../Session/Visuals/VisualReader";

const repoName = (path: string) => path.split(/[\\/]/).filter(Boolean).at(-1) ?? path;
const ago = (at: number) => formatAge(Math.max(0, Date.now() / 1000 - at));

export function KeptVisuals({ client }: { client: KeptVisualsClient }) {
  const [kept, setKept] = useState<KeptVisualDTO[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<KeptVisualDTO | null>(null);
  const seq = useRef(0);
  const load = useCallback(() => {
    const mine = ++seq.current;
    setError(null);
    client.keptVisuals().then(
      (rows) => mine === seq.current && setKept([...rows].sort((a, b) => b.kept_at - a.kept_at)),
      (e: unknown) => mine === seq.current && setError(plain(e instanceof Error ? e.message : String(e))),
    );
  }, [client]);
  useEffect(load, [load]);

  if (open) {
    return (
      <div className="flex h-[70vh] min-h-0 flex-col" data-testid="kept-reader">
        <ReaderChrome
          title={open.title}
          source={{ kind: "kept", id: open.id }}
          meta={`kept ${ago(open.kept_at)} ago · from ${open.from_session_name}`}
          onBack={() => setOpen(null)}
          backLabel="Kept visuals"
          footer={
            open.answer && (
              <p className="border-t border-line px-s2 py-s1 text-sm" role="status">
                Answered <span className="font-semibold">{open.answer.option}</span>
                {open.answer.note && <span className="text-ink-2"> · {open.answer.note}</span>}
              </p>
            )
          }
        />
      </div>
    );
  }
  return (
    <section className="flex flex-col gap-s1" aria-label="Kept visuals" data-testid="kept-visuals">
      <p className="text-xs text-ink-3">Visuals you kept from a session. They stay after the session is removed.</p>
      {error && (
        <div role="alert" className={`${alert} flex items-center justify-between gap-s2`}>
          <span>Couldn't load the kept visuals: {error}</span>
          <button type="button" className={button} onClick={load}>
            Retry
          </button>
        </div>
      )}
      {kept === null && !error && <p className="text-sm text-ink-3">Loading…</p>}
      {kept && kept.length === 0 && <p className="text-sm text-ink-2">Nothing kept yet. Open a visual in a session and press Keep.</p>}
      {kept && kept.length > 0 && (
        <ul className={`${card} flex flex-col divide-y divide-line`}>
          {kept.map((k) => (
            <li key={k.id}>
              <button type="button" className="flex w-full flex-col items-start gap-0.5 px-s1 py-s1 text-left hover:bg-surface-2" data-testid={`kept-${k.id}`} onClick={() => setOpen(k)}>
                <span className="text-sm font-medium text-ink">{k.title}</span>
                <span className="text-xs text-ink-3">
                  {k.from_session_name} · {repoName(k.repo)} · kept {ago(k.kept_at)} ago
                  {k.answer && ` · answered ${k.answer.option}`}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
