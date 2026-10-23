/* Settings' About: the app's version and the license files the bundle carries
   (the shell's about_read, api/tauri.ts): brindle's LICENSE and SCHEDULE-A
   and the third-party notices, each opened one at a time (the notices run to
   a megabyte). A file this build lacks (a dev build) says so; in a browser
   tab, with no shell to read from, the section only names the files. */

import { useEffect, useState } from "react";
import type { About as AboutInfo } from "../../api/tauri";
import { plain } from "../../api/wording";
import { alert, button, card, h2 } from "../../components/ui";

const TITLES: Record<string, string> = {
  LICENSE: "Brindle License",
  "SCHEDULE-A": "Schedule A",
  "THIRD_PARTY_NOTICES.txt": "Third-party notices",
};

export function About({ read }: { read?: () => Promise<AboutInfo> }) {
  const [about, setAbout] = useState<AboutInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => {
    if (!read) return;
    let live = true;
    read().then(
      (a) => {
        if (live) setAbout(a);
      },
      (e: unknown) => {
        if (live) setError(plain(e instanceof Error ? e.message : String(e)));
      },
    );
    return () => {
      live = false;
    };
  }, [read]);
  return (
    <section id="about" aria-labelledby="about-title" className="flex scroll-mt-s2 flex-col gap-s1" data-testid="about">
      <header className="flex flex-wrap items-baseline gap-x-s2 gap-y-0.5">
        <h2 id="about-title" className={h2}>
          About
        </h2>
        <span className="text-xs text-ink-3">{about ? `brindle ${about.version}` : "brindle"}</span>
      </header>
      <div className={`${card} flex flex-col divide-y divide-line`}>
        {!read && <p className="px-s2 py-s2 text-sm text-ink-2">The app carries its license (LICENSE, SCHEDULE-A) and third-party notices; open it to read them here.</p>}
        {error && (
          <p role="alert" className={`${alert} m-s2`}>
            Could not read the license files: {error}
          </p>
        )}
        {about?.files.map((f) => (
          <div key={f.name} className="flex flex-col gap-1 px-s2 py-s2" data-testid={`about-${f.name}`}>
            <div className="flex flex-wrap items-center gap-s2">
              <span className="text-sm font-medium">{TITLES[f.name] ?? f.name}</span>
              <span className="text-xs text-ink-3">{f.name}</span>
              <span className="flex-1" />
              {f.text === null ? (
                <span className="text-xs text-ink-3">Not in this build (a development build carries no {f.name})</span>
              ) : (
                <button type="button" className={button} aria-expanded={open === f.name} onClick={() => setOpen((o) => (o === f.name ? null : f.name))}>
                  {open === f.name ? "Hide" : "Show"}
                </button>
              )}
            </div>
            {open === f.name && f.text !== null && (
              <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded border border-line p-s1 font-mono text-xs" data-testid={`about-${f.name}-text`}>
                {f.text}
              </pre>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
