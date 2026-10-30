/* Knowledge › Docs: the analysis docs agents wrote for large work, from
   brindle Pro's library through the daemon (GET /v1/analysis-docs and its
   topics; nothing is kept on disk). On top, the results panel
   (GET /v1/analysis-docs/results, worked out on this machine): review rounds
   of tasks whose brief drew on the docs against similar tasks without one,
   or "not enough data yet". Below it, scope chips and a search box, a rail
   of topics (the server's, filtered there) and kinds (filtered here), and a
   card per doc. A card opens the reader: the doc exactly as written
   (Markdown, rendered safely) beside what the server read from it, with
   Share, Export and Delete. `session` narrows the list to the docs one
   session wrote or used (the session goal panel links here with it). */

import { useCallback, useEffect, useMemo, useState } from "react";
import { DaemonError, type AnalysisDoc, type AnalysisDocDetail, type DaemonClient, type DocResults, type DocScope, type DocTopic } from "../../api/client";
import type { PickFolder } from "../../api/open";
import { plain } from "../../api/wording";
import { alert, button, card, field, h2, label, notice, pill, quiet } from "../../components/ui";
import { ExportMenu, type ExportClient } from "./ExportMenu";
import { Markdown } from "./Markdown";

export type DocsClient = Pick<DaemonClient, "docs" | "docTopics" | "docScopes" | "doc" | "docResults" | "shareDoc" | "unshareDoc" | "deleteDoc"> & ExportClient;

export interface DocsViewProps {
  client: DocsClient;
  repo?: string;
  /* Only this session's docs (its root agent id). */
  session?: string;
  /* A doc to open in the reader on arrival. */
  doc?: string;
  pickFolder?: PickFolder;
  /* Back to the session the person came from; the filter's chip clears only the filter. */
  onBack?(): void;
}

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

/* A doc's scope as the daemon's filter takes it: user, team:<id> or org:<id>. */
export function scopeSpec(s: DocScope): string {
  if (s.kind === "user") return "user";
  return `${s.kind === "org" ? "org" : "team"}:${s.id}`;
}

/* Its name from the scopes list; a scope known only from a doc is told apart by the start of its id. */
export function scopeName(s: DocScope): string {
  if (s.kind === "user") return "Mine";
  return s.name || (s.kind === "org" ? "Enterprise" : `Team ${s.id.slice(0, 8)}`);
}

/* `had` with each scope of `more` it lacks, in order; a named one (from the scopes list) wins over a bare one. */
const withScopes = (had: DocScope[], more: DocScope[]) => {
  const seen = new Map(had.map((s) => [scopeSpec(s), s]));
  let changed = false;
  for (const s of more) {
    const was = seen.get(scopeSpec(s));
    if (!was || (s.name && !was.name)) {
      seen.set(scopeSpec(s), was ? { ...s, ...was, name: s.name, can_share: s.can_share ?? was.can_share } : s);
      changed = true;
    }
  }
  return changed ? [...seen.values()] : had;
};

const scopeTone = (s?: DocScope) => (!s || s.kind === "user" ? "bg-surface-2 text-ink-2" : s.kind === "org" ? "bg-branch-soft text-branch" : "bg-fawn-soft text-fawn");

/* "Oct 12": a server time (seconds, milliseconds or an ISO string) as a short date. */
export function shortDate(v: number | string | undefined): string | null {
  if (v === undefined || v === null || v === "") return null;
  const ms = typeof v === "number" ? (v < 1e12 ? v * 1000 : v) : Date.parse(v);
  return Number.isNaN(ms) ? null : new Date(ms).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

const notEntitled = (e: unknown) => e instanceof DaemonError && e.code === "not_entitled";

/* The results panel: what the docs did, or that there isn't enough to say yet. */
export function ResultsPanel({ results, error, docs }: { results: DocResults | null; error: string | null; docs: AnalysisDoc[] }) {
  const top = docs
    .filter((d) => (d.used_in_briefs ?? 0) > 0)
    .sort((a, b) => (b.used_in_briefs ?? 0) - (a.used_in_briefs ?? 0))
    .slice(0, 3);
  return (
    <section className={`${card} flex flex-col gap-s2 px-s2 py-s2`} aria-label="Results" data-testid="results">
      <header className="flex flex-wrap items-baseline gap-s1">
        <span className={label}>What your docs did{results ? ` · last ${results.days} days` : ""}</span>
      </header>
      {error && <p className="text-sm text-ink-3">Could not work out the results: {error}</p>}
      {!error && !results && <p className="text-sm text-ink-3">Working it out…</p>}
      {results && !results.enough && (
        <p className="text-sm text-ink-2" data-testid="results-not-enough">
          Not enough data yet. Results show once at least {results.minimum} finished tasks started from a brief and {results.minimum} similar tasks without one can be compared; so far {results.compared} and {results.similar}.
        </p>
      )}
      {results && results.enough && results.with_brief !== null && results.without_brief !== null && (
        <>
          <div className="grid gap-s1 sm:grid-cols-2">
            <div className="flex flex-col gap-0.5 rounded-md border border-line bg-canvas px-s2 py-s1">
              <span className="font-mono text-2xl tabular-nums">{results.briefed}</span>
              <span className="text-sm text-ink-2">tasks started from a brief</span>
              <span className="text-xs text-ink-3">of {plural(results.large, "large task")}</span>
            </div>
            <div className="flex flex-col gap-0.5 rounded-md border border-line bg-canvas px-s2 py-s1">
              <span className="font-mono text-2xl tabular-nums">{results.without_brief > 0 ? `${Math.round(((results.with_brief - results.without_brief) / results.without_brief) * 100)}%` : "—"}</span>
              <span className="text-sm text-ink-2">review rounds</span>
              <span className="text-xs text-ink-3">
                {results.with_brief.toFixed(1)} vs {results.without_brief.toFixed(1)} for similar tasks without one
              </span>
            </div>
          </div>
          <div className="flex flex-col gap-1 text-sm text-ink-2">
            <span className={label}>Review rounds per task</span>
            {[
              ["With a brief", results.with_brief, "bg-fawn"],
              ["Similar tasks, no brief", results.without_brief, "bg-ink-3"],
            ].map(([name, v, tone]) => (
              <div key={name as string} className="grid grid-cols-[10rem_minmax(0,1fr)_3rem] items-center gap-s1">
                <span>{name}</span>
                <span className="h-2.5 rounded-sm bg-line">
                  <span className={`block h-full rounded-sm ${tone}`} style={{ width: `${Math.min(100, ((v as number) / Math.max(results.with_brief!, results.without_brief!, 1)) * 100)}%` }} />
                </span>
                <span className="text-right font-mono text-xs">{(v as number).toFixed(1)}</span>
              </div>
            ))}
            <span className="text-xs text-ink-3">"Similar" means the same kind and size of task, in the same repo.</span>
          </div>
        </>
      )}
      {top.length > 0 && (
        <div className="flex flex-col gap-0.5">
          <span className={label}>Docs used most</span>
          {top.map((d) => (
            <div key={d.id} className="flex justify-between gap-s1 text-sm text-ink-2">
              <span>{d.title || d.id}</span>
              <span className="font-mono text-xs text-ink-3">used in {plural(d.used_in_briefs!, "brief")}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

/* `scope` is the doc's, named from the scopes list when it is there. */
function DocCard({ d, scope, onOpen }: { d: AnalysisDoc; scope?: DocScope; onOpen(): void }) {
  const date = shortDate(d.created_at);
  const findings = d.findings?.length ?? 0;
  const questions = d.open_questions?.length ?? 0;
  return (
    <li data-doc={d.id}>
      <button type="button" className={`${card} flex w-full flex-col gap-1 px-s2 py-s1 text-left hover:border-line-strong`} onClick={onOpen}>
        <span className="flex flex-wrap items-center gap-s1">
          <span className="mr-auto font-semibold">{d.title || d.id}</span>
          {d.kind && <span className={`${pill} border border-line bg-surface-2 text-ink-2`}>{d.kind}</span>}
          {scope && <span className={`${pill} ${scopeTone(scope)}`}>{scopeName(scope)}</span>}
        </span>
        {d.summary && <span className="text-sm text-ink-2">{d.summary}</span>}
        <span className="flex flex-wrap items-center gap-x-s2 text-xs text-ink-3">
          {(date || d.version) && <span>{[date, d.version ? `v${d.version}` : null].filter(Boolean).join(" · ")}</span>}
          {(findings > 0 || questions > 0) && <span>{[findings ? plural(findings, "finding") : null, questions ? plural(questions, "open question") : null].filter(Boolean).join(" · ")}</span>}
          <span className="font-mono">used in {plural(d.used_in_briefs ?? 0, "brief")}</span>
        </span>
      </button>
    </li>
  );
}

interface ReaderProps {
  client: DocsClient;
  repo?: string;
  id: string;
  /* Scopes the person can share into (their own is left out). */
  targets: DocScope[];
  /* Titles of the docs listed, for its connected docs (by id). */
  titles: Map<string, string>;
  pickFolder?: PickFolder;
  onBack(): void;
  onDeleted(): void;
  onOpen(id: string): void;
  /* The scopes the doc turned out to be in, so the view learns teams from it. */
  onScopes(scopes: DocScope[]): void;
}

/* The doc as written, and beside it what the server read from it. */
export function Reader({ client, repo, id, targets, titles, pickFolder, onBack, onDeleted, onOpen, onScopes }: ReaderProps) {
  const [doc, setDoc] = useState<AnalysisDocDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [menu, setMenu] = useState<"share" | "export" | "delete" | null>(null);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);

  const load = useCallback(() => {
    client.doc(id, repo).then(
      (d) => {
        setDoc(d);
        setError(null);
        onScopes([...(d.scope ? [d.scope] : []), ...d.shared_with]);
      },
      (e: unknown) => setError(message(e)),
    );
  }, [client, id, repo, onScopes]);
  useEffect(load, [load]);

  const act = async (what: () => Promise<string>) => {
    setBusy(true);
    setSaid(null);
    try {
      setSaid(await what());
      setMenu(null);
    } catch (e) {
      setSaid(message(e));
    } finally {
      setBusy(false);
    }
  };

  const share = (s: DocScope, on: boolean) =>
    act(async () => {
      if (on) await client.shareDoc(id, scopeSpec(s));
      else await client.unshareDoc(id, scopeSpec(s));
      load();
      return on ? `Shared with ${scopeName(s)}.` : `No longer shared with ${scopeName(s)}.`;
    });

  const remove = () =>
    act(async () => {
      await client.deleteDoc(id);
      onDeleted();
      return "Deleted.";
    });

  const files = doc?.files ?? [];
  const shared = new Set((doc?.shared_with ?? []).map(scopeSpec));
  /* Where it can go (the scopes list's can_share) and where it already is, never its owner's scope. */
  const shareTo = withScopes(targets, doc?.shared_with ?? []).filter(
    (s) => s.kind !== "user" && (!doc?.scope || scopeSpec(doc.scope) !== scopeSpec(s)) && (s.can_share || shared.has(scopeSpec(s))),
  );
  const connected: [string, string][] = [
    ...(doc?.supersedes ?? []).map((d): [string, string] => ["Replaces", d]),
    ...(doc?.superseded_by ? [["Replaced by", doc.superseded_by] as [string, string]] : []),
    ...(doc?.related ?? []).map((d): [string, string] => ["Related", d]),
  ];
  const mentions = doc?.mentions ?? [];

  return (
    <section className="flex flex-col gap-s2" aria-label="Doc" data-testid="reader">
      <div className="flex flex-wrap items-center justify-between gap-s1">
        <button type="button" className={quiet} onClick={onBack}>
          ← Docs
        </button>
        <div className="relative flex flex-wrap gap-1">
          <button type="button" className={button} disabled={!doc || busy} aria-expanded={menu === "share"} onClick={() => setMenu(menu === "share" ? null : "share")}>
            Share
          </button>
          <button type="button" className={button} disabled={!doc || busy} aria-expanded={menu === "export"} onClick={() => setMenu(menu === "export" ? null : "export")}>
            Export ▾
          </button>
          <button type="button" className={`${quiet} text-sap`} disabled={!doc || busy} onClick={() => setMenu(menu === "delete" ? null : "delete")}>
            Delete
          </button>
        </div>
      </div>
      {menu === "share" && (
        <div className={`${card} flex flex-wrap items-center gap-1 px-s2 py-s1 text-sm`} role="group" aria-label="Share with">
          {shareTo.length === 0 && <span className="text-ink-2">There's no team or organisation you can share into.</span>}
          {shareTo.map((s) => {
            const on = shared.has(scopeSpec(s));
            return (
              <button key={scopeSpec(s)} type="button" className={button} disabled={busy} onClick={() => void share(s, !on)}>
                {on ? `Stop sharing with ${scopeName(s)}` : `Share with ${scopeName(s)}`}
              </button>
            );
          })}
        </div>
      )}
      {menu === "export" && <ExportMenu client={client} id={id} title={doc?.title} repo={repo} pickFolder={pickFolder} onSaid={setSaid} />}
      {menu === "delete" && (
        <div role="alert" className={`${alert} flex flex-wrap items-center justify-between gap-s1`}>
          <span>Delete this doc from the library? Briefs stop using it. This can't be undone.</span>
          <button type="button" className={button} disabled={busy} onClick={() => void remove()}>
            Delete for good
          </button>
        </div>
      )}
      {said && (
        <p role="status" className={notice}>
          {said}
        </p>
      )}
      {error && (
        <div role="alert" className={`${alert} flex items-center justify-between gap-s2`}>
          <span>Could not open the doc: {error}</span>
          <button type="button" className={button} onClick={load}>
            Try again
          </button>
        </div>
      )}
      {!doc && !error && <p className="text-sm text-ink-3">Opening…</p>}
      {doc && (
        <div className="grid gap-s2 lg:grid-cols-[minmax(0,1fr)_16rem]">
          <article className={`${card} flex min-w-0 flex-col gap-s2 px-s3 py-s2`} data-testid="doc-body">
            <h2 className="text-xl font-semibold tracking-tight">{doc.title || doc.id}</h2>
            {files.map((f, i) => (
              <div key={`${f.name}${i}`} className="flex flex-col gap-s1">
                {files.length > 1 && f.name && <span className={`${label} border-t border-line pt-s1`}>{f.name}</span>}
                <Markdown text={f.md} />
              </div>
            ))}
          </article>
          <aside className="flex min-w-0 flex-col gap-s1 text-sm" aria-label="What brindle read from it">
            {doc.summary && (
              <div className={`${card} flex flex-col gap-1 border-branch/40 px-s2 py-s1`}>
                <span className="flex items-center justify-between">
                  <span className={label}>At a glance</span>
                  <span className="font-mono text-[10.5px] uppercase text-ink-3">Generated</span>
                </span>
                <p className="text-ink-2">{doc.summary}</p>
              </div>
            )}
            {(doc.findings?.length ?? 0) > 0 && (
              <div className={`${card} flex flex-col gap-1 px-s2 py-s1`}>
                <span className={label}>Findings</span>
                <ul className="flex list-disc flex-col gap-0.5 pl-s2 text-ink-2">
                  {doc.findings!.map((f, i) => (
                    <li key={i}>{f}</li>
                  ))}
                </ul>
              </div>
            )}
            {(doc.open_questions?.length ?? 0) > 0 && (
              <div className={`${card} flex flex-col gap-1 px-s2 py-s1`}>
                <span className={label}>{doc.open_questions!.length === 1 ? "Open question" : "Open questions"}</span>
                <ul className="flex list-disc flex-col gap-0.5 pl-s2 text-sap">
                  {doc.open_questions!.map((q, i) => (
                    <li key={i}>{q}</li>
                  ))}
                </ul>
              </div>
            )}
            {(connected.length > 0 || mentions.length > 0) && (
              <div className={`${card} flex flex-col gap-1 px-s2 py-s1`}>
                <span className={label}>Connected</span>
                {connected.length > 0 && (
                  <ul className="flex flex-col gap-0.5 text-ink-2">
                    {connected.map(([relation, other]) => (
                      <li key={`${relation}${other}`}>
                        {relation}:{" "}
                        <button type="button" className="text-left text-ink underline decoration-line-strong hover:decoration-ink" data-connected={other} onClick={() => onOpen(other)}>
                          {titles.get(other) ?? other}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
                {mentions.length > 0 && <span className="text-xs text-ink-3">Mentions: {mentions.join(" · ")}</span>}
              </div>
            )}
            <div className={`${card} flex flex-col gap-1 px-s2 py-s1`}>
              <span className={label}>Used in briefs</span>
              <span className="text-xs text-ink-3">{(doc.used_in_briefs ?? 0) === 0 ? "Not yet" : plural(doc.used_in_briefs!, "time")}</span>
            </div>
          </aside>
        </div>
      )}
    </section>
  );
}

type Listing = { state: "loading" } | { state: "loaded"; docs: AnalysisDoc[] } | { state: "unavailable"; error: string } | { state: "failed"; error: string };

export function DocsView({ client, repo, session: initialSession, doc, pickFolder, onBack }: DocsViewProps) {
  const [scope, setScope] = useState("");
  const [topic, setTopic] = useState("");
  const [kind, setKind] = useState("");
  const [query, setQuery] = useState("");
  const [session, setSession] = useState(initialSession);
  const [listing, setListing] = useState<Listing>({ state: "loading" });
  const [topics, setTopics] = useState<DocTopic[]>([]);
  const [results, setResults] = useState<DocResults | null>(null);
  const [resultsError, setResultsError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(doc ?? null);
  /* The scopes the person reads (GET /analysis-docs/scopes: theirs, their teams', their
     org's, named, with can_share), plus any other a listed or opened doc is in; a chip
     stays while another is picked. */
  const [scopes, setScopes] = useState<DocScope[]>([]);
  const learn = useCallback((more: DocScope[]) => setScopes((had) => withScopes(had, more)), []);

  useEffect(() => {
    client.docScopes(repo).then(
      (got) => learn(got.scopes),
      () => undefined, // without them the chips are just All and Mine
    );
  }, [client, repo, learn]);

  const load = useCallback(() => {
    let live = true;
    setListing({ state: "loading" });
    client.docs({ repo, scope: scope || undefined, topic: topic || undefined, session }).then(
      (got) => {
        if (!live) return;
        setListing({ state: "loaded", docs: got.docs });
        learn(got.docs.flatMap((d) => (d.scope ? [d.scope] : [])));
      },
      (e: unknown) => live && setListing({ state: notEntitled(e) ? "unavailable" : "failed", error: message(e) }),
    );
    return () => {
      live = false;
    };
  }, [client, repo, scope, topic, session]);
  useEffect(load, [load]);

  useEffect(() => {
    let live = true;
    client.docTopics({ repo, scope: scope || undefined }).then(
      (got) => live && setTopics(got.topics),
      () => live && setTopics([]),
    );
    return () => {
      live = false;
    };
  }, [client, repo, scope]);

  useEffect(() => {
    let live = true;
    client.docResults(repo).then(
      (r) => {
        if (!live) return;
        setResults(r);
        setResultsError(null);
      },
      (e: unknown) => live && setResultsError(message(e)),
    );
    return () => {
      live = false;
    };
  }, [client, repo]);

  const docs = listing.state === "loaded" ? listing.docs : [];
  const kinds = useMemo(() => {
    const counts = new Map<string, number>();
    for (const d of docs) if (d.kind) counts.set(d.kind, (counts.get(d.kind) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [docs]);
  const q = query.trim().toLowerCase();
  const shown = docs.filter((d) => (!kind || d.kind === kind) && (!q || [d.title, d.summary, ...(d.topics ?? [])].some((t) => t?.toLowerCase().includes(q))));
  const chips: [string, string][] = [["", "All"], ["user", "Mine"], ...scopes.filter((s) => s.kind !== "user").map((s): [string, string] => [scopeSpec(s), scopeName(s)])];

  if (open) {
    return (
      <Reader
        key={open}
        client={client}
        repo={repo}
        id={open}
        targets={scopes}
        titles={new Map(docs.map((d) => [d.id, d.title || d.id]))}
        pickFolder={pickFolder}
        onOpen={setOpen}
        onScopes={learn}
        onBack={() => setOpen(null)}
        onDeleted={() => {
          setOpen(null);
          load();
        }}
      />
    );
  }

  const rail = (title: string, items: [string, number | undefined][], on: string, pick: (v: string) => void) => (
    <div className="flex flex-col gap-0.5">
      <span className={`${label} px-s1 pb-1`}>{title}</span>
      {items.map(([name, count]) => (
        <button
          key={name}
          type="button"
          aria-pressed={on === name}
          className={`flex justify-between rounded-md px-s1 py-1 text-left text-sm ${on === name ? "bg-surface font-medium text-ink shadow-[inset_0_0_0_1px_var(--color-line)]" : "text-ink-2 hover:bg-surface-2"}`}
          onClick={() => pick(on === name ? "" : name)}
        >
          <span>{name}</span>
          {count !== undefined && <span className="font-mono text-xs text-ink-3">{count}</span>}
        </button>
      ))}
    </div>
  );

  return (
    <section className="flex flex-col gap-s2" aria-label="Docs" data-testid="docs">
      <ResultsPanel results={results} error={resultsError} docs={docs} />
      {(session || onBack) && (
        <div className="flex flex-wrap items-center gap-s1" data-testid="session-filter">
          {session && (
            <span className={`${pill} inline-flex items-center gap-1 bg-branch-soft text-branch`}>
              Session {session}
              <button type="button" className="rounded-sm hover:text-ink" aria-label="Clear session filter" onClick={() => setSession(undefined)}>
                ×
              </button>
            </span>
          )}
          {session && <span className="text-xs text-ink-3">Showing the docs one session wrote or used.</span>}
          <span className="flex-1" />
          {onBack && (
            <button type="button" className={`${quiet} text-xs`} onClick={onBack}>
              Back to session
            </button>
          )}
        </div>
      )}
      <div className="flex flex-wrap items-center justify-between gap-s1">
        <input type="search" className={`${field} min-w-56 flex-1`} aria-label="Search docs" placeholder="Search docs: titles, summaries, topics…" value={query} onChange={(e) => setQuery(e.target.value)} />
        <div className="flex flex-wrap gap-1" role="group" aria-label="Scope">
          {chips.map(([spec, name]) => (
            <button key={spec || "all"} type="button" aria-pressed={scope === spec} className={`rounded-full border px-s1 py-0.5 text-xs ${scope === spec ? "border-transparent bg-fawn-soft font-medium text-ink" : "border-line text-ink-2 hover:bg-surface-2"}`} onClick={() => setScope(spec)}>
              {name}
            </button>
          ))}
        </div>
      </div>
      {listing.state === "unavailable" && (
        <div className={`${card} flex flex-col gap-1 px-s2 py-s2 text-sm`} data-testid="docs-unavailable">
          <h2 className={h2}>Docs live in brindle Pro's library</h2>
          <p className="text-ink-2">Large work leaves a doc of what was built, decided and left open. With brindle Pro it's stored encrypted in your library and briefs the next related task. On the free plan the docs are written to ~/.brindle/docs and show up under Imported.</p>
          <p className="text-xs text-ink-3">{listing.error}</p>
        </div>
      )}
      {listing.state === "failed" && (
        <div role="alert" className={`${alert} flex items-center justify-between gap-s2`}>
          <span>Could not load the docs: {listing.error}</span>
          <button type="button" className={button} onClick={load}>
            Try again
          </button>
        </div>
      )}
      {listing.state !== "unavailable" && (
        <div className="grid gap-s2 md:grid-cols-[12rem_minmax(0,1fr)]">
          <nav className="flex flex-col gap-s2" aria-label="Topics and kinds">
            {rail(
              "Topics",
              topics.map((t) => [t.name, t.count]),
              topic,
              setTopic,
            )}
            {kinds.length > 0 && rail("Kinds", kinds, kind, setKind)}
          </nav>
          <div className="flex min-w-0 flex-col gap-s1">
            {listing.state === "loading" && <p className="text-sm text-ink-3">Loading…</p>}
            {listing.state === "loaded" && docs.length === 0 && <p className="text-sm text-ink-2">No docs here yet. Large work (a goal with several milestones, or a heavy task) leaves one when its branch merges.</p>}
            {listing.state === "loaded" && docs.length > 0 && shown.length === 0 && <p className="text-sm text-ink-2">No doc matches.</p>}
            {shown.length > 0 && (
              <ul className="flex flex-col gap-s1">
                {shown.map((d) => (
                  <DocCard key={d.id} d={d} scope={d.scope && (scopes.find((s) => scopeSpec(s) === scopeSpec(d.scope!)) ?? d.scope)} onOpen={() => setOpen(d.id)} />
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
