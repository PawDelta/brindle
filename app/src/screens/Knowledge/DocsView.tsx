/* The Library's designs (library-redesign option A, states 1 and 2): a title naming what the
   sidebar picked (All designs, a stage, a repo or a folder) with a List / Board switch, one search
   box, one filter row (scope, author, reviewer, when it changed) and then the designs, as a table
   (screens/Library/LibraryList) or as columns by stage (LibraryBoard). Both views are the same
   Library: switching keeps the search, the filters and the folder picked, and the view is
   remembered on this device. Stage lives only in the sidebar, scope only in the filter row. A
   search also reaches repo decisions, indexed document passages and kept visuals, listed under
   the designs. The foot says in one line what the docs did. A row or card opens the design's
   saved text beside its record (Reader). */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { DaemonError, type AnalysisDoc, type AnalysisDocDetail, type DaemonClient, type DocResults, type DocScope, type FolderGroup } from "../../api/client";
import type { PickFolder } from "../../api/open";
import type { RepoDTO } from "../../api/schema";
import { plain } from "../../api/wording";
import { alert, button, card, h2, label, notice, pill, quiet } from "../../components/ui";
import { ChevronDownIcon, SearchIcon, ShareIcon } from "../../components/icons";
import { ExportMenu, type ExportClient } from "./ExportMenu";
import { Markdown } from "./Markdown";
import { docMatch, useLibrarySearch, type LibraryHit, type SearchClient } from "./librarySearch";
import { ReaderChrome } from "../Session/Visuals/VisualReader";
import { setReaderDoc, useLibraryChanges, useLibraryPicks } from "../../store/libraryReader";
import { LIBRARY_STAGES, type LibraryStage } from "../../routes";
import { atStage, folderOf, groupDocs, loadRecords, placedFolders, repoName, signOffRows, type DesignRecord, type GroupBy, type Queue } from "../Library/libraryStages";
import { LibraryList } from "../Library/LibraryList";
import { LibraryBoard } from "../Library/LibraryBoard";

export type DocsClient = Pick<DaemonClient, "docs" | "docScopes" | "doc" | "docResults" | "shareDoc" | "unshareDoc" | "deleteDoc"> & Partial<Pick<DaemonClient, "libraryFolders">> & ExportClient & SearchClient;

export interface DocsViewProps {
  client: DocsClient;
  /* The repo whose account reads the Library (the docs themselves are every repo's). */
  repo?: string;
  /* Only this session's docs (its root agent id). */
  session?: string;
  /* A doc to open in the reader on arrival. */
  doc?: string;
  pickFolder?: PickFolder;
  /* What the sidebar picked: a stage, a folder (libraryStages.folderKey) or a repo (its root). */
  stage?: LibraryStage;
  folder?: string;
  inRepo?: string;
  /* What the person's own queue says of each design (libraryStages.docQueue). */
  queue?: Queue;
  /* The repos the app knows, for their names. */
  repos?: Pick<RepoDTO, "name" | "repo_root">[] | null;
  /* Back to the session the person came from; the filter's chip clears only the filter. */
  onBack?(): void;
  /* A design opens in the design reader (Session/Visuals/SharedDesignReader), here in the Library:
     Back comes back to this list as it was. `design`: `doc` is one, before the list says so. */
  designReader?(id: string, title: string | undefined, onBack: () => void): ReactNode;
  design?: boolean;
}

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

const VIEW_KEY = "brindle.library.view";
type Layout = "list" | "board";
/* List or Board, as the person last left it on this device. */
function readLayout(): Layout {
  try {
    return localStorage.getItem(VIEW_KEY) === "board" ? "board" : "list";
  } catch {
    return "list";
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
const EMPTY_QUEUE: Queue = new Map();
const NO_FOLDERS: FolderGroup[] = [];

/* A filter as a chip: its name, or what is picked (then in the accent), over a native select
   laid on top unseen, so the chip is as wide as what it says rather than its longest option. */
function ChipSelect({ name, value, options, onChange }: { name: string; value: string; options: [string, string][]; onChange(value: string): void }) {
  const shown = options.find(([id]) => id === value)?.[1];
  return (
    <span className={`relative inline-flex items-center gap-[5px] rounded-full border px-[9px] py-0.5 text-xs focus-within:outline-2 focus-within:outline-branch ${value ? "border-transparent bg-branch-soft text-branch" : "border-line text-ink-2 hover:bg-surface-2"}`}>
      <span className="max-w-[14rem] truncate">{shown ?? name}</span>
      <ChevronDownIcon size={14} className={value ? "text-branch" : "text-ink-3"} />
      <select aria-label={name} value={value} onChange={(e) => onChange(e.target.value)} className="absolute inset-0 cursor-pointer appearance-none opacity-0">
        <option value="">{value ? `Any ${name.toLowerCase()}` : name}</option>
        {options.map(([id, text]) => <option key={id} value={id}>{text}</option>)}
      </select>
    </span>
  );
}

/* A segmented choice: one button pressed. */
function Seg<T extends string>({ name, value, options, onChange }: { name: string; value: T; options: [T, string][]; onChange(value: T): void }) {
  return (
    <span role="group" aria-label={name} className="inline-flex overflow-hidden rounded border border-line-strong text-xs">
      {options.map(([id, text]) => (
        <button key={id || "all"} type="button" aria-pressed={value === id} className={`px-2.5 py-0.5 ${value === id ? "bg-branch-soft font-medium text-branch" : "text-ink-3 hover:text-ink"}`} onClick={() => onChange(id)}>
          {text}
        </button>
      ))}
    </span>
  );
}

const signed = (n: number) => `${n < 0 ? "−" : "+"}${Math.abs(n)}%`;

export function DocsView({ client, repo, session: initialSession, doc, pickFolder, stage, folder, inRepo, queue = EMPTY_QUEUE, repos, onBack, designReader, design }: DocsViewProps) {
  const [layout, setLayout] = useState<Layout>(readLayout);
  const pickLayout = (next: Layout) => {
    setLayout(next);
    try {
      localStorage.setItem(VIEW_KEY, next);
    } catch {
      // No storage: the view stays as picked for this window.
    }
  };
  const [scope, setScope] = useState("");
  const [query, setQuery] = useState("");
  const [author, setAuthor] = useState("");
  const [changed, setChanged] = useState("");
  const [groupBy, setGroupBy] = useState<GroupBy>("folder");
  const [showResults, setShowResults] = useState(false);
  const [sourceOpen, setSourceOpen] = useState<LibraryHit | null>(null);
  const [session, setSession] = useState(initialSession);
  const [listing, setListing] = useState<Listing>({ state: "loading" });
  const [results, setResults] = useState<DocResults | null>(null);
  const [resultsError, setResultsError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(doc ?? null);
  // A row picked in the sidebar (a stage, a repo, a folder, All designs, even the one already picked)
  // closes whatever is open here and shows what was picked; the search and filters stay. Going to
  // Settings and back is not a pick: the design stays open.
  const picks = useLibraryPicks();
  const [seenPicks, setSeenPicks] = useState(picks);
  if (picks !== seenPicks) {
    setSeenPicks(picks);
    setOpen(null);
    setSourceOpen(null);
  }
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

  useEffect(() => {
    client.docScopes(repo).then(
      (got) => learn(got.scopes),
      () => undefined, // without them the chips are just All and Mine
    );
  }, [client, repo, learn]);

  const load = useCallback(() => {
    let live = true;
    setListing({ state: "loading" });
    client.docs({ repo, scope: scope || undefined, session }).then(
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
  }, [client, repo, scope, session]);
  useEffect(load, [load]);

  /* The folders, for the groups' names and the folder or repo the sidebar picked; read again with
     every design's record when the sidebar makes a folder or moves a design (libraryChanged). */
  const changes = useLibraryChanges();
  const [folderGroups, setFolderGroups] = useState<FolderGroup[]>(NO_FOLDERS);
  useEffect(() => {
    let live = true;
    client.libraryFolders?.().then(
      (got) => live && setFolderGroups(Array.isArray(got?.scopes) ? got.scopes : NO_FOLDERS),
      () => live && setFolderGroups(NO_FOLDERS),
    );
    return () => {
      live = false;
    };
  }, [client, changes]);

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
  /* What each listed design's detail says (its review, its plan's tasks, its folder): read once per
     listing, and again after a change in the sidebar. */
  const [records, setRecords] = useState<Map<string, DesignRecord>>(() => new Map());
  useEffect(() => {
    let live = true;
    if (docs.length === 0) { setRecords(new Map()); return; }
    loadRecords(client, docs, repo).then((got) => live && setRecords(got));
    return () => {
      live = false;
    };
  }, [client, docs, repo, changes]);
  const placed = useMemo(() => placedFolders(folderGroups), [folderGroups]);
  const placeOf = useCallback((id: string) => {
    const key = folderOf(records.get(id));
    return key ? placed.find((f) => f.key === key) : undefined;
  }, [records, placed]);
  const [reviewer, setReviewer] = useState("");
  const reviewers = [...new Set([...records.values()].flatMap((r) => (r.design?.reviewers ?? []).map((x) => x.sub)))];
  const q = query.trim().toLowerCase();
  const narrowed = !!(scope || session || stage || folder || inRepo);
  // The Sources are searched only for a query, over the whole Library.
  const search = useLibrarySearch(client, docs, repo, q, !narrowed && !!q);
  const authors = [...new Set(docs.flatMap((d) => d.created_by ? [d.created_by] : []))];
  const recent = (at: number | undefined) => !changed || (at !== undefined && at >= Date.now() / 1000 - Number(changed) * 86400);
  const inPlace = (d: AnalysisDoc) => (folder ? folderOf(records.get(d.id)) === folder : !inRepo || placeOf(d.id)?.repo === inRepo);
  const matches = new Map(docs.map((d) => [d.id, docMatch(d, search.details?.get(d.id), q)]));
  const shown = docs.filter((d) => (!author || d.created_by === author) && (!stage || atStage(stage, d.id, queue, records.get(d.id))) && (!reviewer || !!records.get(d.id)?.design?.reviewers?.some((r) => r.sub === reviewer)) && recent(d.updated_at ?? d.created_at) && inPlace(d) && matches.get(d.id) !== null);
  // Decisions, imported passages and kept visuals show only for a search, over the whole Library.
  const sourceHits = q ? search.hits.filter((d) => !reviewer && (!author || author === d.author) && recent(d.changed)) : [];
  const groups = groupDocs(shown, groupBy, queue, records, placed, repos);
  const chips: [string, string][] = [["", "Everyone's"], ["user", "Mine"], ...scopes.filter((s) => s.kind !== "user").map((s): [string, string] => [scopeSpec(s), scopeName(s)])];
  const filtered = !!(scope || author || reviewer || changed || query);
  const clear = () => { setQuery(""); setScope(""); setAuthor(""); setReviewer(""); setChanged(""); setSession(undefined); };

  const picked = folder ? placed.find((f) => f.key === folder) : undefined;
  const heading = picked ? picked.name : inRepo ? repoName(inRepo, repos) : stage ? LIBRARY_STAGES.find((s) => s.id === stage)!.label : "All designs";
  const repoCount = new Set(shown.flatMap((d) => placeOf(d.id)?.repo ?? [])).size;
  const counted = `${shown.length}${repoCount > 1 ? ` in ${repoCount} repos` : ""}`;

  // ⌘K (Ctrl+K) finds: it goes to the search box.
  const box = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const find = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k" && box.current) {
        e.preventDefault();
        box.current.focus();
      }
    };
    window.addEventListener("keydown", find);
    return () => window.removeEventListener("keydown", find);
  }, []);

  const scroll = (node: ReactNode) => <div className="flex min-h-0 flex-1 flex-col overflow-auto p-s3">{node}</div>;
  if (sourceOpen?.kept) return <div className="flex min-h-0 flex-1 flex-col p-s3"><ReaderChrome title={sourceOpen.title} meta={sourceOpen.kept.from_session_name} source={{ kind: "kept", id: sourceOpen.kept.id }} onBack={() => setSourceOpen(null)} backLabel="Library" /></div>;
  if (sourceOpen) return scroll(<Reader client={client} repo={repo} id={sourceOpen.id} targets={[]} titles={new Map()} local={{ title: sourceOpen.title, markdown: sourceOpen.text }} onBack={() => setSourceOpen(null)} onDeleted={() => undefined} onOpen={setOpen} onScopes={learn} />);

  const listed = open ? docs.find((d) => d.id === open) : undefined;
  if (open && designReader && (listed?.kind === "design" || (design && open === doc))) return designReader(open, listed?.title, () => setOpen(null));
  if (open) {
    return scroll(
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
      />,
    );
  }

  return (
    <section className={`flex min-h-0 flex-1 flex-col ${layout === "board" && shown.length > 0 ? "overflow-hidden" : "overflow-auto"}`} aria-label="Docs" data-testid="docs" data-layout={layout}>
      {(session || onBack) && (
        <div className="flex flex-wrap items-center gap-s1 px-8 pt-s2" data-testid="session-filter">
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
      <div className="flex items-center gap-3 px-8 pt-5">
        <h2 className="text-[22px] font-semibold tracking-[-0.015em]">{heading}</h2>
        {listing.state === "loaded" && <span className="text-xs text-ink-3" data-testid="library-count">{counted}</span>}
        {picked?.private && <span className="text-xs text-ink-3">only you see this folder</span>}
        <span className="flex-1" />
        <Seg<Layout> name="View" value={layout} options={[["list", "List"], ["board", "Board"]]} onChange={pickLayout} />
      </div>
      <label className="mx-8 mt-3.5 flex items-center gap-2.5 border-b border-line-strong px-0.5 py-1.5 text-ink-3">
        <SearchIcon size={14} />
        <input ref={box} type="search" className="flex-1 border-0 bg-transparent text-sm text-ink outline-none placeholder:text-ink-3" aria-label="Search the library" placeholder="Search designs, comments, decisions and imported docs" value={query} onChange={(e) => setQuery(e.target.value)} />
        <kbd className="rounded-[3px] border border-line-strong px-1 font-mono text-[10.5px] text-ink-3">⌘K</kbd>
      </label>
      <div className="flex flex-wrap items-center gap-1.5 px-8 pb-3.5 pt-3" role="group" aria-label="Library filters">
        <Seg name="Scope" value={scope} options={chips} onChange={setScope} />
        {authors.length > 0 && <ChipSelect name="Author" value={author} options={authors.map((a) => [a, a])} onChange={setAuthor} />}
        {reviewers.length > 0 && <ChipSelect name="Reviewer" value={reviewer} options={reviewers.map((r) => [r, r])} onChange={setReviewer} />}
        <ChipSelect name="Changed" value={changed} options={[["1", "Last day"], ["7", "Last week"], ["30", "Last month"]]} onChange={setChanged} />
        {filtered && <button type="button" className={`${quiet} text-xs`} onClick={clear}>Clear</button>}
        <span className="flex-1" />
        {layout === "list" ? (
          <>
            <span className="text-xs text-ink-3">Newest first · group by</span>
            <Seg<GroupBy> name="Group by" value={groupBy} options={[["folder", "Folder"], ["stage", "Stage"], ["none", "None"]]} onChange={setGroupBy} />
          </>
        ) : (
          <span className="text-xs text-ink-3">Newest first in each column</span>
        )}
      </div>
      {(q || search.searching || search.incomplete) && <p role="status" className="px-8 pb-2 text-xs text-ink-3">{search.searching ? "Searching…" : `${shown.length + sourceHits.length} matches`}{search.incomplete && " · Some sources could not be searched."}</p>}
      {listing.state === "unavailable" && (
        <div className={`${card} mx-8 flex flex-col gap-1 py-s2 text-sm`} data-testid="docs-unavailable">
          <h2 className={h2}>No designs available</h2>
          <p className="text-ink-2">Plans you approve and designs you share are saved here, with the record of what was agreed and why.</p>
          <p className="text-xs text-ink-3">{listing.error}</p>
        </div>
      )}
      {listing.state === "failed" && (
        <div role="alert" className={`${alert} mx-8 flex items-center justify-between gap-s2`}>
          <span>Could not load the library: {listing.error}</span>
          <button type="button" className={button} onClick={load}>
            Try again
          </button>
        </div>
      )}
      {listing.state === "loading" && <p className="px-8 text-sm text-ink-3">Loading…</p>}
      {listing.state === "loaded" && docs.length === 0 && sourceHits.length === 0 && !search.searching && !q && <div data-testid="library-empty" className={`${card} mx-8 flex flex-col gap-s1 py-s3`}><h2 className={h2}>No designs saved yet</h2><p className="text-sm text-ink-2">Plans you approve and designs you share are saved here, with the record of what was agreed and why.</p><ol className="list-decimal pl-s2 text-sm text-ink-2"><li>Ask for a plan in a session.</li><li>Review it and approve the design.</li><li>Find it here when you need it again.</li></ol></div>}
      {listing.state === "loaded" && (docs.length > 0 || q) && shown.length === 0 && sourceHits.length === 0 && !search.searching && <p className="px-8 text-sm text-ink-2">No design matches.</p>}
      {shown.length > 0 && layout === "list" && <LibraryList groups={groups} queue={queue} records={records} scopes={scopes} matches={q ? matches : undefined} onOpen={setOpen} />}
      {shown.length > 0 && layout === "board" && (
        <LibraryBoard docs={shown} queue={queue} records={records} onOpen={setOpen} folderOf={(id) => {
          const f = placeOf(id);
          return f && { label: f.repo ? `${repoName(f.repo, repos)} / ${f.name}` : f.name, private: f.private };
        }} />
      )}
      {sourceHits.length > 0 && (
        <section aria-label="Also in the Library" className="px-8 pt-s2">
          <h3 className={`${label} pb-1`}>Also in the Library</h3>
          <ul className="flex flex-col divide-y divide-line">{sourceHits.map((hit) => <li key={hit.id} data-library-hit={hit.id}><button type="button" className="flex w-full flex-col gap-1 py-s1 text-left hover:bg-surface-2" onClick={() => setSourceOpen(hit)}><b className="text-[13.5px] font-semibold">{hit.title}</b><span className="text-xs text-ink-3">{hit.type === "imported" ? "Imported doc · matching passage" : hit.type === "kept" ? "Kept visual" : "Decision"}{hit.changed && ` · ${shortDate(hit.changed)}`}</span><span className="line-clamp-2 text-[12.5px] text-ink-2">{hit.text}</span></button></li>)}</ul>
        </section>
      )}
      {(results || resultsError) && (
        <>
          {showResults && <div className="px-8 pt-s2"><ResultsPanel results={results} error={resultsError} docs={docs} /></div>}
          <div className="sticky bottom-0 z-[2] mx-8 mt-auto flex flex-wrap items-center gap-[18px] border-t border-line bg-canvas pb-3.5 pt-3 text-[12.5px] text-ink-3" data-testid="results-line">
            {results && <span className={label}>Last {results.days} days</span>}
            {results?.enough && results.with_brief !== null && results.without_brief !== null ? (
              <>
                <span><b className="font-mono text-sm font-semibold text-ink">{results.briefed}</b> tasks started from a design's brief</span>
                {results.without_brief > 0 && <span><b className="font-mono text-sm font-semibold text-ink">{signed(Math.round(((results.with_brief - results.without_brief) / results.without_brief) * 100))}</b> review rounds against similar tasks without one</span>}
              </>
            ) : (
              <span>{resultsError ? "Could not work out what your docs did." : "Not enough finished tasks yet to say what your docs did."}</span>
            )}
            <span className="flex-1" />
            <button type="button" className={`${quiet} text-xs`} aria-expanded={showResults} onClick={() => setShowResults(!showResults)}>What your docs did {showResults ? "▾" : "▸"}</button>
          </div>
        </>
      )}
    </section>
  );
}
