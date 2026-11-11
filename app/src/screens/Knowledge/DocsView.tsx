/* Library: search above rows of saved designs, with the existing scope,
   category, topic and session filters. Whole-Library search also reaches
   repo decisions, indexed document passages and kept visuals. A row opens
   the saved text beside its available record. Results live below the list;
   unavailable review/folder metadata is not presented as a working control. */

import { useCallback, useEffect, useMemo, useState } from "react";
import { DaemonError, type AnalysisDoc, type AnalysisDocDetail, type DaemonClient, type DocResults, type DocScope, type DocTopic } from "../../api/client";
import type { PickFolder } from "../../api/open";
import { plain } from "../../api/wording";
import { alert, button, card, field, h2, label, notice, pill, quiet } from "../../components/ui";
import { ChevronDownIcon, ShareIcon } from "../../components/icons";
import { ExportMenu, type ExportClient } from "./ExportMenu";
import { Markdown } from "./Markdown";
import { docMatch, useLibrarySearch, type LibraryHit, type LibraryType, type SearchClient } from "./librarySearch";
import { ReaderChrome } from "../Session/Visuals/VisualReader";
import { setReaderDoc } from "../../store/libraryReader";
import { LIBRARY_STAGES, type LibraryStage } from "../../routes";
import { loadDesigns, signOffRows, stageOfDoc, type DesignReview } from "./libraryStages";

export type DocsClient = Pick<DaemonClient, "docs" | "docTopics" | "docScopes" | "doc" | "docResults" | "shareDoc" | "unshareDoc" | "deleteDoc"> & ExportClient & SearchClient;

export interface DocsViewProps {
  client: DocsClient;
  repo?: string;
  /* Only this session's docs (its root agent id). */
  session?: string;
  /* A doc to open in the reader on arrival. */
  doc?: string;
  pickFolder?: PickFolder;
  /* The stage the sidebar chose (the Stage filter starts there), and the stage of each design the
     Inbox places (libraryStages.docStages); a design not in the map has no stage. */
  stage?: LibraryStage;
  stages?: Map<string, LibraryStage>;
  /* Back to the session the person came from; the filter's chip clears only the filter. */
  onBack?(): void;
}

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

const UNCATEGORISED = "Uncategorised";
const COLLAPSED_KEY = "brindle.docs.collapsed";

/* Docs under their categories: the largest first, then by name, and the
   docs without one last. */
export function byCategory(docs: AnalysisDoc[]): [string, AnalysisDoc[]][] {
  const groups = new Map<string, AnalysisDoc[]>();
  for (const d of docs) {
    const name = d.category ?? UNCATEGORISED;
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name)!.push(d);
  }
  return [...groups].sort(([a, x], [b, y]) => Number(a === UNCATEGORISED) - Number(b === UNCATEGORISED) || y.length - x.length || a.localeCompare(b));
}

/* The categories collapsed in a repo's list, kept in this device's storage. */
function readCollapsed(repo = ""): Set<string> {
  try {
    const got: unknown = JSON.parse(localStorage.getItem(COLLAPSED_KEY) ?? "{}");
    const list: unknown = got && typeof got === "object" ? (got as Record<string, unknown>)[repo] : null;
    return new Set(Array.isArray(list) ? list.filter((s): s is string => typeof s === "string") : []);
  } catch {
    return new Set();
  }
}

function writeCollapsed(repo = "", collapsed: Set<string>): void {
  try {
    const got: unknown = JSON.parse(localStorage.getItem(COLLAPSED_KEY) ?? "{}");
    const all = got && typeof got === "object" ? (got as Record<string, unknown>) : {};
    localStorage.setItem(COLLAPSED_KEY, JSON.stringify({ ...all, [repo]: [...collapsed] }));
  } catch {
    // No storage: the headers stay as they are for this window.
  }
}

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
function DocRow({ d, scope, match, onOpen }: { d: AnalysisDoc; scope?: DocScope; match?: string | null; onOpen(): void }) {
  const date = shortDate(d.updated_at ?? d.created_at);
  const findings = d.findings?.length ?? 0;
  const questions = d.open_questions?.length ?? 0;
  return (
    <li data-doc={d.id}>
      <button type="button" className="grid w-full grid-cols-[minmax(0,1fr)_7rem_6rem] items-center gap-s2 border-b border-line px-s2 py-s2 text-left hover:bg-surface-2" onClick={onOpen}>
        <span className="flex min-w-0 flex-col gap-1">
        <span className="flex flex-wrap items-center gap-s1">
          <span className="mr-auto font-semibold">{d.title || d.id}</span>
          {d.kind && <span className={`${pill} border border-line bg-surface-2 text-ink-2`}>{d.kind}</span>}
          {scope && <span className={`${pill} ${scopeTone(scope)}`}>{scopeName(scope)}</span>}
        </span>
        {(match || d.summary) && <span className="line-clamp-2 text-sm text-ink-2">{match || d.summary}</span>}
        <span className="flex flex-wrap items-center gap-x-s2 text-xs text-ink-3">
          {(date || d.version) && <span>{[date, d.version ? `v${d.version}` : null].filter(Boolean).join(" · ")}</span>}
          {(findings > 0 || questions > 0) && <span>{[findings ? plural(findings, "finding") : null, questions ? plural(questions, "open question") : null].filter(Boolean).join(" · ")}</span>}
          <span className="font-mono">used in {plural(d.used_in_briefs ?? 0, "brief")}</span>
        </span>
        </span>
        <span className="truncate text-xs text-ink-3">{d.source?.session ? `Session ${d.source.session}` : "—"}</span>
        <span className="text-right text-xs text-ink-3">{date ?? "—"}</span>
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
  /* A locally indexed document, shown from its own text rather than fetched as an analysis doc; it can't be shared, exported or deleted. */
  local?: { title: string; markdown: string };
}

/* The doc as written, and beside it what the server read from it. */
export function Reader({ client, repo, id, targets, titles, pickFolder, onBack, onDeleted, onOpen, onScopes, local }: ReaderProps) {
  const [doc, setDoc] = useState<AnalysisDocDetail | null>(
    local ? ({ id, title: local.title, files: [{ name: "", md: local.markdown }], shared_with: [] } as unknown as AnalysisDocDetail) : null,
  );
  const [error, setError] = useState<string | null>(null);
  const [menu, setMenu] = useState<"share" | "export" | "delete" | null>(null);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);

  const load = useCallback(() => {
    if (local) return;
    let live = true;
    client.doc(id, repo).then(
      (d) => {
        if (!live) return;
        setDoc(d);
        setError(null);
        onScopes([...(d.scope ? [d.scope] : []), ...d.shared_with]);
      },
      (e: unknown) => live && setError(message(e)),
    );
    return () => { live = false; };
  }, [client, id, repo, onScopes, local]);
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
  const taken = doc?.verdict?.approved ? (doc.decisions ?? []).flatMap((d) => {
    const pick = doc.verdict?.picks?.[d.id] || d.mine;
    return pick ? [{ decision: d, pick }] : [];
  }) : [];

  return (
    <section className="flex flex-col gap-s2" aria-label="Doc" data-testid="reader">
      <div className="flex flex-wrap items-center justify-between gap-s1">
        <button type="button" className={quiet} onClick={onBack}>
          ← Library
        </button>
        {!local && <div className="relative flex flex-wrap gap-1">
          <button type="button" className={button} disabled={!doc || busy} aria-expanded={menu === "share"} onClick={() => setMenu(menu === "share" ? null : "share")}>
            Share
          </button>
          <button type="button" className={`${button} pl-2.5 pr-2`} disabled={!doc || busy} aria-haspopup="true" aria-expanded={menu === "export"} onClick={() => setMenu(menu === "export" ? null : "export")}>
            <ShareIcon size={15} className="text-ink-2" />
            Export
            <ChevronDownIcon size={14} className={`text-ink-3 transition-transform duration-150 ${menu === "export" ? "rotate-180" : ""}`} />
          </button>
          <button type="button" className={`${quiet} text-sap`} disabled={!doc || busy} onClick={() => setMenu(menu === "delete" ? null : "delete")}>
            Delete
          </button>
        </div>}
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
          <aside className="flex min-w-0 flex-col gap-s1 text-sm" aria-label="Design record">
            {!!doc.revisions?.length && <section className={`${card} flex flex-col gap-1 px-s2 py-s1`} aria-label="Revisions">
              <h3 className={label}>Revisions</h3>
              {doc.revisions.map((r) => <div key={r.rev} data-revision={r.rev}><b>rev {r.rev}</b><span className="text-ink-3"> · {r.author} · {shortDate(r.at)}</span><p>{r.source}{r.changed.length > 0 && ` · ${r.changed.join(", ")}`}</p></div>)}
            </section>}
            {taken.length > 0 && <section className={`${card} flex flex-col gap-1 px-s2 py-s1`} aria-label="Decisions taken">
              <h3 className={label}>Decisions taken</h3>
              {taken.map(({ decision: d, pick }) => <div key={d.id} data-decision={d.id}><b>{d.title}</b><p>{d.options.find((o) => o.id === pick)?.label ?? pick}</p></div>)}
            </section>}
            {signOffRows(doc.design).length > 0 && <section className={`${card} flex flex-col gap-1 px-s2 py-s1`} aria-label="Sign-offs">
              <h3 className={label}>Sign-offs</h3>
              {signOffRows(doc.design).map((r) => <div key={`${r.sub}@${r.rev ?? "waiting"}`} data-signoff={r.sub} data-signed={r.signed ? "yes" : "no"} data-rev={r.rev ?? ""}><b>{r.sub}</b><span className="text-ink-3"> · {r.role}{r.rev !== undefined ? ` · signed rev ${r.rev}${r.signed ? "" : " (earlier)"}` : " · waiting"}</span></div>)}
            </section>}
            {!!doc.threads?.length && <section className={`${card} flex flex-col gap-1 px-s2 py-s1`} aria-label="Comments">
              <h3 className={label}>Comments</h3>
              {doc.threads.map((t) => <div key={t.id}><b>{t.author}</b><p>{t.text}</p>{t.replies.map((r, i) => <p key={i} className="pl-s2 text-ink-2">{r.text}</p>)}</div>)}
            </section>}
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
            {doc.used_in_briefs !== undefined && <div className={`${card} flex flex-col gap-1 px-s2 py-s1`}>
              <span className={label}>Used in briefs</span>
              <span className="text-xs text-ink-3">{(doc.used_in_briefs ?? 0) === 0 ? "Not yet" : plural(doc.used_in_briefs!, "time")}</span>
            </div>}
          </aside>
        </div>
      )}
    </section>
  );
}

type Listing = { state: "loading" } | { state: "loaded"; docs: AnalysisDoc[] } | { state: "unavailable"; error: string } | { state: "failed"; error: string };
const EMPTY_DOCS: AnalysisDoc[] = [];
const EMPTY_QUEUE = new Map<string, LibraryStage>();

export function DocsView({ client, repo, session: initialSession, doc, pickFolder, stage: initialStage, stages, onBack }: DocsViewProps) {
  const [stage, setStage] = useState<LibraryStage | "">(initialStage ?? "");
  const [scope, setScope] = useState("");
  const [topic, setTopic] = useState("");
  const [category, setCategory] = useState("");
  const [query, setQuery] = useState("");
  const [type, setType] = useState<LibraryType>("all");
  const [author, setAuthor] = useState("");
  const [changed, setChanged] = useState("");
  const [sourceOpen, setSourceOpen] = useState<LibraryHit | null>(null);
  const [session, setSession] = useState(initialSession);
  const [listing, setListing] = useState<Listing>({ state: "loading" });
  const [topics, setTopics] = useState<DocTopic[]>([]);
  const [results, setResults] = useState<DocResults | null>(null);
  const [resultsError, setResultsError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(doc ?? null);
  // The Library rail moves the design that is open here, so it is told which, and when none.
  useEffect(() => {
    setReaderDoc(sourceOpen ? null : open);
  }, [open, sourceOpen]);
  useEffect(() => () => setReaderDoc(null), []);
  /* The scopes the person reads (GET /analysis-docs/scopes: theirs, their teams', their
     org's, named, with can_share), plus any other a listed or opened doc is in; a chip
     stays while another is picked. */
  const [scopes, setScopes] = useState<DocScope[]>([]);
  const learn = useCallback((more: DocScope[]) => setScopes((had) => withScopes(had, more)), []);
  /* The collapsed headers belong to the repo they were read for; another
     repo reads its own. */
  const [stored, setStored] = useState(() => ({ repo, collapsed: readCollapsed(repo) }));
  const collapsed = stored.repo === repo ? stored.collapsed : readCollapsed(repo);
  const setOpenHeader = (name: string, open: boolean) => {
    const next = new Set(collapsed);
    if (open) next.delete(name);
    else next.add(name);
    setStored({ repo, collapsed: next });
    writeCollapsed(repo, next);
  };
  const toggle = (name: string) => setOpenHeader(name, collapsed.has(name));
  /* Picking a category in the rail opens its header once; the header then
     collapses as any other. */
  const pickCategory = (name: string) => {
    setCategory(name);
    if (name && collapsed.has(name)) setOpenHeader(name, true);
  };

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
      (e: unknown) => {
        if (!live) return;
        setResults(null);
        setResultsError(e instanceof DaemonError && e.status === 404 ? null : message(e));
      },
    );
    return () => {
      live = false;
    };
  }, [client, repo]);

  const docs = listing.state === "loaded" ? listing.docs : EMPTY_DOCS;
  /* The review state of each listed design (its detail's `design`), read once per listing. */
  const [designs, setDesigns] = useState<Map<string, DesignReview>>(() => new Map());
  useEffect(() => {
    let live = true;
    if (docs.length === 0) { setDesigns(new Map()); return; }
    loadDesigns(client, docs, repo).then((got) => live && setDesigns(got));
    return () => {
      live = false;
    };
  }, [client, docs, repo]);
  const [reviewer, setReviewer] = useState("");
  const reviewers = [...new Set([...designs.values()].flatMap((d) => (d.reviewers ?? []).map((r) => r.sub)))];
  const queue = stages ?? EMPTY_QUEUE;
  const q = query.trim().toLowerCase();
  const search = useLibrarySearch(client, docs, repo, q, !scope && !topic && !category && !session);
  const authors = [...new Set(docs.flatMap((d) => d.created_by ? [d.created_by] : []))];
  const recent = (at: number | undefined) => !changed || (at !== undefined && at >= Date.now() / 1000 - Number(changed) * 86400);
  /* The rail's categories, in the list's order, when the server gave any. */
  const categories = useMemo((): [string, number][] => (docs.some((d) => d.category) ? byCategory(docs).map(([name, list]) => [name, list.length]) : []), [docs]);
  /* A picked category that the listing no longer has filters nothing, and is
     dropped once that listing has loaded, so no doc is hidden behind a
     filter the rail can't show. */
  const picked = categories.some(([name]) => name === category) ? category : "";
  useEffect(() => {
    if (listing.state === "loaded" && category && !picked) setCategory("");
  }, [listing.state, category, picked]);
  const shown = docs.filter((d) => (type === "all" || type === "designs") && (!author || d.created_by === author) && (!stage || stageOfDoc(d.id, queue, designs.get(d.id)) === stage) && (!reviewer || !!designs.get(d.id)?.reviewers?.some((r) => r.sub === reviewer)) && recent(d.updated_at ?? d.created_at) && (!picked || (d.category ?? UNCATEGORISED) === picked) && docMatch(d, search.details?.get(d.id), q) !== null)
    .sort((a, b) => (b.updated_at ?? b.created_at ?? 0) - (a.updated_at ?? a.created_at ?? 0));
  const sourceHits = search.hits.filter((d) => (type === "all" || type === d.type) && !stage && !reviewer && (!author || author === d.author) && recent(d.changed));
  const docRow = (d: AnalysisDoc) => (
    <DocRow key={d.id} d={d} match={docMatch(d, search.details?.get(d.id), q)} scope={d.scope && (scopes.find((s) => scopeSpec(s) === scopeSpec(d.scope!)) ?? d.scope)} onOpen={() => setOpen(d.id)} />
  );
  const chips: [string, string][] = [["", "All"], ["user", "Mine"], ...scopes.filter((s) => s.kind !== "user").map((s): [string, string] => [scopeSpec(s), scopeName(s)])];

  if (sourceOpen?.kept) return <div className="flex h-[70vh] flex-col"><ReaderChrome title={sourceOpen.title} meta={sourceOpen.kept.from_session_name} source={{ kind: "kept", id: sourceOpen.kept.id }} onBack={() => setSourceOpen(null)} backLabel="Library" /></div>;
  if (sourceOpen) return <Reader client={client} repo={repo} id={sourceOpen.id} targets={[]} titles={new Map()} local={{ title: sourceOpen.title, markdown: sourceOpen.text }} onBack={() => setSourceOpen(null)} onDeleted={() => undefined} onOpen={setOpen} onScopes={learn} />;

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
        <input type="search" className={`${field} min-w-56 flex-1`} aria-label="Search the library" placeholder="Search designs, comments, decisions and docs…" value={query} onChange={(e) => setQuery(e.target.value)} />
        {(scope || topic || category || session) && <button type="button" className={quiet} onClick={() => { setScope(""); setTopic(""); setCategory(""); setSession(undefined); }}>Search whole Library</button>}
        <div className="flex flex-wrap gap-1" role="group" aria-label="Scope">
          {chips.map(([spec, name]) => (
            <button key={spec || "all"} type="button" aria-pressed={scope === spec} className={`rounded-full border px-s1 py-0.5 text-xs ${scope === spec ? "border-transparent bg-fawn-soft font-medium text-ink" : "border-line text-ink-2 hover:bg-surface-2"}`} onClick={() => setScope(spec)}>
              {name}
            </button>
          ))}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-s1" aria-label="Library filters">
        <label className="text-sm">Type <select aria-label="Type" className={field} value={type} onChange={(e) => setType(e.target.value as LibraryType)}>
          <option value="all">All</option><option value="designs">Designs</option>
          {client.decisions && <option value="decisions">Decisions</option>}
          {client.kbSearch && <option value="imported">Imported docs</option>}
          {client.keptVisuals && <option value="kept">Kept visuals</option>}
        </select></label>
        <label className="text-sm">Shared <select aria-label="Shared" className={field} value={scope} onChange={(e) => setScope(e.target.value)}>{chips.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>
        <label className="text-sm">Stage <select aria-label="Stage" className={field} value={stage} onChange={(e) => setStage(e.target.value as LibraryStage | "")}><option value="">Any stage</option>{LIBRARY_STAGES.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}</select></label>
        {reviewers.length > 0 && <label className="text-sm">Reviewer <select aria-label="Reviewer" className={field} value={reviewer} onChange={(e) => setReviewer(e.target.value)}><option value="">Anyone</option>{reviewers.map((r) => <option key={r}>{r}</option>)}</select></label>}
        {authors.length > 0 && <label className="text-sm">Author <select aria-label="Author" className={field} value={author} onChange={(e) => setAuthor(e.target.value)}><option value="">Anyone</option>{authors.map((a) => <option key={a}>{a}</option>)}</select></label>}
        <label className="text-sm">Changed <select aria-label="Changed" className={field} value={changed} onChange={(e) => setChanged(e.target.value)}><option value="">Any time</option><option value="1">Last day</option><option value="7">Last week</option><option value="30">Last month</option></select></label>
        <button type="button" className={quiet} onClick={() => { setQuery(""); setType("all"); setStage(""); setReviewer(""); setScope(""); setTopic(""); setCategory(""); setAuthor(""); setChanged(""); setSession(undefined); }}>Clear</button>
      </div>
      {(q || search.searching || search.incomplete) && <p role="status" className="text-xs text-ink-3">{search.searching ? "Searching…" : `${shown.length + sourceHits.length} matches`}{search.incomplete && " · Some sources could not be searched."}</p>}
      {!q && type === "imported" && <p className="text-sm text-ink-2">Enter a search to find imported docs.</p>}
      {listing.state === "unavailable" && (
        <div className={`${card} flex flex-col gap-1 px-s2 py-s2 text-sm`} data-testid="docs-unavailable">
          <h2 className={h2}>No designs available</h2>
          <p className="text-ink-2">Plans you approve and designs you share are saved here, with the record of what was agreed and why.</p>
          <p className="text-xs text-ink-3">{listing.error}</p>
        </div>
      )}
      {listing.state === "failed" && (
        <div role="alert" className={`${alert} flex items-center justify-between gap-s2`}>
          <span>Could not load the library: {listing.error}</span>
          <button type="button" className={button} onClick={load}>
            Try again
          </button>
        </div>
      )}
      {(listing.state !== "unavailable" || sourceHits.length > 0) && (
        <div className="grid gap-s2 md:grid-cols-[12rem_minmax(0,1fr)]">
          <nav className="flex flex-col gap-s2" aria-label="Topics and kinds">
            {categories.length > 0 && rail("Categories", categories, picked, pickCategory)}
            {rail(
              "Topics",
              topics.map((t) => [t.name, t.count]),
              topic,
              setTopic,
            )}
          </nav>
          <div className="flex min-w-0 flex-col gap-s1">
            {listing.state === "loading" && <p className="text-sm text-ink-3">Loading…</p>}
            {listing.state === "loaded" && docs.length === 0 && sourceHits.length === 0 && !search.searching && !q && <div data-testid="library-empty" className={`${card} flex flex-col gap-s1 px-s3 py-s3`}><h2 className={h2}>No designs saved yet</h2><p className="text-sm text-ink-2">Plans you approve and designs you share are saved here, with the record of what was agreed and why.</p><ol className="list-decimal pl-s2 text-sm text-ink-2"><li>Ask for a plan in a session.</li><li>Review it and approve the design.</li><li>Find it here when you need it again.</li></ol></div>}
            {listing.state === "loaded" && (docs.length > 0 || q) && shown.length === 0 && sourceHits.length === 0 && !search.searching && <p className="text-sm text-ink-2">No doc matches.</p>}
            {shown.length > 0 && <div className="grid grid-cols-[minmax(0,1fr)_7rem_6rem] gap-s2 border-b border-line px-s2 text-xs text-ink-3"><span>Design</span><span>Session</span><span className="text-right">Changed</span></div>}
            {shown.length > 0 && !shown.some((d) => d.category) && <ul className="flex flex-col">{shown.map(docRow)}</ul>}
            {shown.some((d) => d.category) && byCategory(shown).map(([name, list]) => {
              const closed = collapsed.has(name);
              return (
                <section key={name} aria-label={name} className="flex flex-col gap-s1">
                  <button type="button" aria-expanded={!closed} onClick={() => toggle(name)} className="flex items-center gap-1.5 border-b border-line pb-1 pt-s2 text-left text-[15px] font-semibold text-ink hover:text-branch">
                    <span aria-hidden className={`inline-block w-3 text-ink-3 transition-transform ${closed ? "" : "rotate-90"}`}>›</span>
                    {name}
                    <span className="text-sm font-normal text-ink-3">{list.length}</span>
                  </button>
                  {!closed && <ul className="flex flex-col gap-s1">{list.map(docRow)}</ul>}
                </section>
              );
            })}
            {sourceHits.length > 0 && <ul className="flex flex-col divide-y divide-line">{sourceHits.map((hit) => <li key={hit.id} data-library-hit={hit.id}><button type="button" className="flex w-full flex-col gap-1 px-s2 py-s2 text-left hover:bg-surface-2" onClick={() => setSourceOpen(hit)}><b>{hit.title}</b><span className="text-xs text-ink-3">{hit.type === "imported" ? "Imported doc · matching passage" : hit.type === "kept" ? "Kept visual" : "Decision"}{hit.changed && ` · ${shortDate(hit.changed)}`}</span><span className="line-clamp-2 text-sm text-ink-2">{hit.text}</span></button></li>)}</ul>}
          </div>
        </div>
      )}
      {(results || resultsError) && <details className="border-t border-line pt-s1"><summary className="cursor-pointer text-xs text-ink-3">What your docs did</summary><ResultsPanel results={results} error={resultsError} docs={docs} /></details>}
    </section>
  );
}
