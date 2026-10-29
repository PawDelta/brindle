/* Knowledge: what a repo's agents know and remember about it, said in one
   line at the top. Two kinds. Decisions (GET /v1/decisions): the rules
   every agent follows, each a card with what was decided, why, when, its
   state and the paths it covers, and a Record button for a new one
   (POST /v1/decisions). A topic already decided for the same paths is
   refused by the engine; the refusal names the decision in the way, and
   one click records the new one in its place (supersedes). Documents
   (GET /v1/kb/sources): the folders agents read, each with what it is, how
   many documents are indexed and when it was added; a folder is added
   from the native picker (or a typed path) with POST /v1/kb/sources, indexed
   again with POST /v1/kb/sources/{id}/sync (both jobs, followed on the bus
   to their job.done / job.failed) and removed, in one click, with
   DELETE /v1/kb/sources/{id} (engine branch desktop/kb-sources). One
   search box covers both: decisions by what they say (the topic / text /
   rationale match `brindle decisions --query` makes) and the documents
   through the engine's own search (GET /v1/kb/search, the same ranking
   agents get). A repo with nothing yet gets an empty state that says how
   knowledge gets here. Nothing here sends anyone to a terminal. */

import { useCallback, useContext, useEffect, useRef, useState, type FormEvent } from "react";
import { DaemonError, type DaemonClient, type DecisionPut, type KbHit, type KbSource } from "../../api/client";
import type { BusEvent } from "../../api/events";
import type { PickFolder } from "../../api/open";
import type { DecisionDTO } from "../../api/schema";
import { plain } from "../../api/wording";
import { PageHeader } from "../../components/PageHeader";
import { NO_REPO, RepoSelect, useRepo } from "../../components/RepoSelect";
import { alert, button, card, field, h2, main, mono, notice, pill, quiet } from "../../components/ui";
import { formatAge } from "../../store/overview";
import { StoreContext, useAppState } from "../../store/useStore";

export interface KnowledgeScreenProps {
  client: DaemonClient;
  /* The native folder picker (the app's shell); without one the path is typed. */
  pickFolder?: PickFolder;
}

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

/* knowledge.decisions' `query`: a decision matches when its topic, text or
   rationale contains the words, case-insensitively. */
export function matches(d: DecisionDTO, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [d.topic, d.body, d.rationale].some((text) => text.toLowerCase().includes(q));
}

/* The decision an engine refusal names: "...already covers these paths (abcdef12); pass supersedes..." */
export function refusedBy(text: string): string | null {
  const m = /\(([0-9a-f]{8})\)/.exec(text);
  return m ? m[1] : null;
}

/* What a source is, for the person: a folder's name, or its kind. */
export function sourceName(s: KbSource): string {
  if (s.kind === "folder" && s.scope) return s.scope.replace(/\/+$/, "").split("/").pop() || s.scope;
  return s.scope ?? s.kind;
}

export function sourceLine(s: KbSource): string {
  const what = s.kind === "folder" ? "Folder" : s.kind;
  return s.scope ? `${what} · ${s.scope}` : what;
}

/* "6d ago", "just now": a time since, in words. */
const ago = (ts: number) => {
  const age = formatAge(Date.now() / 1000 - ts);
  return age === "just now" ? age : `${age} ago`;
};

function DecisionCard({ d }: { d: DecisionDTO }) {
  return (
    <li className={`${card} flex flex-col gap-s1 px-s2 py-s2`} data-decision={d.id} data-topic={d.topic}>
      <div className="flex flex-wrap items-baseline gap-x-s1 gap-y-0.5">
        <span className="font-semibold">{d.topic}</span>
        <span className={`${pill} ${d.status === "accepted" ? "text-ink-2" : "text-ink-3"}`}>{d.status === "accepted" ? "In force" : d.status}</span>
        <span className="flex-1" />
        <span className="text-xs text-ink-3" title={d.id}>
          {ago(d.created_at)}
        </span>
      </div>
      <p className="text-sm">{d.body}</p>
      {d.rationale && (
        <p className="text-sm text-ink-2">
          <span className="font-medium text-ink-3">Why: </span>
          {d.rationale}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-1 text-xs text-ink-3">
        <span>Applies to</span>
        {d.scope.length === 0 ? (
          <span className={`${pill} text-ink-2`}>the whole repo</span>
        ) : (
          d.scope.map((p) => (
            <span key={p} className={`${pill} ${mono} text-ink-2`}>
              {p}
            </span>
          ))
        )}
      </div>
    </li>
  );
}

/* Record a decision: topic, the decision, why, and the paths it covers. */
function RecordForm({ client, repo, onRecorded, onCancel }: { client: DaemonClient; repo: string; onRecorded(told: string[]): void; onCancel(): void }) {
  const [topic, setTopic] = useState("");
  const [decision, setDecision] = useState("");
  const [rationale, setRationale] = useState("");
  const [paths, setPaths] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /* The decision the engine refused to record over; one click supersedes it. */
  const [inTheWay, setInTheWay] = useState<string | null>(null);

  const body = (supersedes?: string): DecisionPut => ({
    repo,
    topic: topic.trim(),
    decision: decision.trim(),
    rationale: rationale.trim() || undefined,
    paths: paths
      .split(/[,\n]/)
      .map((p) => p.trim())
      .filter(Boolean),
    supersedes,
  });

  const record = async (supersedes?: string) => {
    setBusy(true);
    setError(null);
    try {
      const got = await client.recordDecision(body(supersedes));
      onRecorded(got.told);
    } catch (e) {
      const text = message(e);
      setError(text);
      setInTheWay(e instanceof DaemonError && e.code === "conflict" ? refusedBy(text) : null);
    } finally {
      setBusy(false);
    }
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    void record();
  };

  const row = "grid gap-x-s2 gap-y-0.5 md:grid-cols-[9rem_minmax(0,1fr)] md:items-start";
  return (
    <form aria-label="Record a decision" onSubmit={submit} className={`${card} flex flex-col gap-s1 border-pass/55 px-s2 py-s2`} data-testid="record-form">
      <div className="flex items-baseline gap-s1">
        <span className="font-semibold">Record a decision</span>
        <span className="text-xs text-ink-3">A rule for every agent in this repo, from now on. Every session is told; new tasks on these paths carry it in their brief.</span>
      </div>
      <div className={row}>
        <label htmlFor="decision-topic" className="text-sm font-medium">
          Topic
        </label>
        <div className="flex flex-col gap-0.5">
          <input id="decision-topic" className={field} aria-label="Topic" placeholder="storage" value={topic} required autoFocus onChange={(e) => setTopic(e.target.value)} />
          <span className="text-xs text-ink-3">One or two words naming what this is about. One decision per topic and set of paths.</span>
        </div>
      </div>
      <div className={row}>
        <label htmlFor="decision-body" className="text-sm font-medium">
          What was decided
        </label>
        <div className="flex flex-col gap-0.5">
          <textarea id="decision-body" className={`${field} min-h-16 resize-y`} aria-label="Decision" placeholder="Use SQLite for the cache." value={decision} required rows={2} onChange={(e) => setDecision(e.target.value)} />
          <span className="text-xs text-ink-3">One line an agent can follow.</span>
        </div>
      </div>
      <div className={row}>
        <label htmlFor="decision-why" className="text-sm font-medium">
          Why
        </label>
        <div className="flex flex-col gap-0.5">
          <input id="decision-why" className={field} aria-label="Why" placeholder="Optional, but worth a line: it stops the next person undoing it." value={rationale} onChange={(e) => setRationale(e.target.value)} />
        </div>
      </div>
      <div className={row}>
        <label htmlFor="decision-paths" className="text-sm font-medium">
          Applies to
        </label>
        <div className="flex flex-col gap-0.5">
          <input id="decision-paths" className={`${field} font-mono`} aria-label="Paths" placeholder="src/cache/**, src/db/**" value={paths} onChange={(e) => setPaths(e.target.value)} />
          <span className="text-xs text-ink-3">Paths or globs, comma-separated. Leave empty for the whole repo.</span>
        </div>
      </div>
      {error && (
        <div role="alert" className={`${alert} flex flex-wrap items-center justify-between gap-s1`}>
          <span>{error}</span>
          {inTheWay && (
            <button type="button" className={button} disabled={busy} onClick={() => void record(inTheWay)}>
              Replace {inTheWay}
            </button>
          )}
        </div>
      )}
      <div className="flex items-center justify-end gap-s1">
        <button type="button" className={quiet} disabled={busy} onClick={onCancel}>
          Cancel
        </button>
        <button type="submit" className={main} disabled={busy || !topic.trim() || !decision.trim()}>
          {busy ? "Recording…" : "Record"}
        </button>
      </div>
    </form>
  );
}

/* Add a folder of documents: its path, from the native picker where the
   app has one or typed, and whether it is just this person's. */
function AddFolderForm({ pickFolder, busy, onAdd, onCancel }: { pickFolder?: PickFolder; busy: boolean; onAdd(path: string, isPrivate: boolean): void; onCancel(): void }) {
  const [path, setPath] = useState("");
  const [isPrivate, setPrivate] = useState(false);
  const [picking, setPicking] = useState(false);
  const choose = async () => {
    if (!pickFolder) return;
    setPicking(true);
    try {
      const picked = await pickFolder("Add a folder of documents");
      if (picked) setPath(picked);
    } finally {
      setPicking(false);
    }
  };
  return (
    <form
      aria-label="Add a folder"
      className={`${card} flex flex-col gap-s1 border-pass/55 px-s2 py-s2`}
      onSubmit={(e) => {
        e.preventDefault();
        if (path.trim()) onAdd(path.trim(), isPrivate);
      }}
    >
      <div className="flex items-baseline gap-s1">
        <span className="font-semibold">Add a folder</span>
        <span className="text-xs text-ink-3">Markdown or text files in it (and its subfolders) are indexed; agents get the parts that fit their task.</span>
      </div>
      <div className="flex flex-wrap items-center gap-s1">
        {pickFolder && (
          <button type="button" className={button} disabled={busy || picking} onClick={() => void choose()}>
            {picking ? "Choosing…" : "Choose folder…"}
          </button>
        )}
        <input className={`${field} min-w-64 flex-1 font-mono`} aria-label="Folder path" placeholder={pickFolder ? "or type a path: ~/Notes/design" : "~/Notes/design"} value={path} disabled={busy} onChange={(e) => setPath(e.target.value)} />
        <label className="flex items-center gap-1 text-sm">
          <input type="checkbox" checked={isPrivate} disabled={busy} onChange={(e) => setPrivate(e.target.checked)} />
          Just for me
        </label>
        <button type="button" className={quiet} disabled={busy} onClick={onCancel}>
          Cancel
        </button>
        <button type="submit" className={button} disabled={busy || !path.trim()}>
          Add
        </button>
      </div>
    </form>
  );
}

/* kb.STALE_DAYS: a hit from a document untouched this long is marked. */
const STALE_DAYS = 180;

function Hit({ h }: { h: KbHit }) {
  const stale = h.updated_at != null && Date.now() / 1000 - h.updated_at > STALE_DAYS * 86400;
  return (
    <li className="flex flex-col gap-0.5 py-s1" data-hit={h.chunk_id}>
      <div className="flex flex-wrap items-baseline gap-x-s1 text-sm">
        <span className="font-medium">{h.title || h.external_id}</span>
        {h.heading_path && <span className="text-ink-3">› {h.heading_path}</span>}
        {h.updated && <span className="text-xs text-ink-3">· updated {h.updated}</span>}
        {stale && <span className={`${pill} text-sap`}>not updated in {STALE_DAYS} days</span>}
      </div>
      <p className="line-clamp-4 whitespace-pre-wrap text-sm text-ink-2">{h.text}</p>
      {h.url && (
        <span className={`${mono} truncate text-xs text-ink-3`} title={h.url}>
          {h.url}
        </span>
      )}
    </li>
  );
}

/* The documents' hits for the query, as the engine ranks them. */
type Hits = { state: "idle" } | { state: "searching" } | { state: "found"; hits: KbHit[] } | { state: "failed"; error: string };

/* A job of the engine's this screen started and still follows (an add, a re-index). */
interface Pending {
  job: string;
  label: string;
}

function Source({ s, busy, indexing, onSync, onRemove }: { s: KbSource; busy: boolean; indexing: boolean; onSync(): void; onRemove(): void }) {
  const indexed = indexing ? "Indexing…" : s.docs === undefined || s.docs === 0 ? "Not indexed yet" : s.docs === 1 ? "1 document indexed" : `${s.docs} documents indexed`;
  return (
    <li className="flex flex-wrap items-center gap-x-s2 gap-y-1 px-s2 py-s1" data-source={s.id} data-indexing={indexing || undefined}>
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <div className="flex flex-wrap items-baseline gap-x-s1">
          <span className="font-medium">{sourceName(s)}</span>
          <span className="text-xs text-ink-3">{s.kind === "folder" ? "Folder" : s.kind}</span>
          {s.visibility && <span className={`${pill} text-ink-2`}>{s.visibility === "private" ? "just for me" : s.visibility}</span>}
          {s.quarantined && (
            <span className={`${pill} text-sap`} title="Some of its text looked like instructions to an agent, or a secret. Agents never see those parts.">
              some text held back
            </span>
          )}
        </div>
        {s.scope && (
          <span className={`${mono} min-w-0 break-all text-xs text-ink-3`} title={s.id}>
            {s.scope}
          </span>
        )}
        <div className="flex flex-wrap items-center gap-x-s1 text-xs text-ink-3">
          <span>{indexed}</span>
          {s.added_at != null && <span>· added {ago(s.added_at)}</span>}
          {s.credential_name && <span>· uses key {s.credential_name}</span>}
          {s.retention_days != null && <span>· kept {s.retention_days} days</span>}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <button type="button" className={quiet} aria-label={`Re-index ${sourceName(s)}`} disabled={busy || indexing} onClick={onSync}>
          Re-index
        </button>
        <button type="button" className={`${quiet} text-sap`} aria-label={`Remove ${sourceName(s)}`} disabled={busy || indexing} onClick={onRemove}>
          Remove
        </button>
      </div>
    </li>
  );
}

/* How knowledge gets here, for a repo with none yet. */
function EmptyState({ onRecord, onAddFolder }: { onRecord(): void; onAddFolder(): void }) {
  return (
    <section className={`${card} flex flex-col gap-s2 px-s3 py-s3`} aria-label="How knowledge gets here" data-testid="empty-state">
      <div>
        <h2 className={h2}>Nothing here yet</h2>
        <p className="text-sm text-ink-2">Knowledge is what your agents know and remember about this repo. It builds up three ways:</p>
      </div>
      <ol className="flex flex-col gap-s1 text-sm">
        <li className="flex gap-s2">
          <span className="w-5 shrink-0 font-semibold text-pass">1</span>
          <span>
            <span className="font-medium">You record a decision.</span> A rule every agent follows from now on, with why. Workers get the ones that cover their files in their brief.
          </span>
        </li>
        <li className="flex gap-s2">
          <span className="w-5 shrink-0 font-semibold text-pass">2</span>
          <span>
            <span className="font-medium">A session records one.</span> A supervisor that settles something durable keeps it here for every later session.
          </span>
        </li>
        <li className="flex gap-s2">
          <span className="w-5 shrink-0 font-semibold text-pass">3</span>
          <span>
            <span className="font-medium">You add a folder of documents.</span> Markdown or text (design notes, runbooks) that agents can look up and get the relevant parts of.
          </span>
        </li>
      </ol>
      <div className="flex flex-wrap gap-s1">
        <button type="button" className={main} onClick={onRecord}>
          Record a decision
        </button>
        <button type="button" className={button} onClick={onAddFolder}>
          Add folder
        </button>
      </div>
    </section>
  );
}

export function KnowledgeScreen({ client, pickFolder }: KnowledgeScreenProps) {
  const pick = useRepo();
  const { repo, repos } = pick;
  const [decisions, setDecisions] = useState<DecisionDTO[] | null>(null);
  const [sources, setSources] = useState<KbSource[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<Hits>({ state: "idle" });
  const [recording, setRecording] = useState(false);
  const [recorded, setRecorded] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [sourceError, setSourceError] = useState<string | null>(null);
  /* The engine's jobs still running, by id, and the sources they index. */
  const [pending, setPending] = useState<Pending[]>([]);
  const [indexing, setIndexing] = useState<Record<string, string>>({});
  // Decisions are recorded by sessions too (decision.recorded on the bus): refetch on every event.
  const lastEventId = useAppState((s) => s.lastEventId);
  const recent = useAppState((s) => s.recent);
  const store = useContext(StoreContext);
  if (!store) throw new Error("KnowledgeScreen needs a <StoreContext.Provider>");

  // Only the newest load for the repo on screen applies.
  const seq = useRef(0);
  const load = useCallback(() => {
    if (!repo) return Promise.resolve();
    const mine = ++seq.current;
    return Promise.all([client.decisions(repo), client.kbSources(repo)]).then(
      ([ds, ss]) => {
        if (mine !== seq.current) return;
        setDecisions(ds);
        setSources(ss);
        setLoadError(null);
      },
      (e: unknown) => {
        if (mine === seq.current) setLoadError(message(e));
      },
    );
  }, [client, repo]);

  useEffect(() => {
    setDecisions(null);
    setSources(null);
    setRecorded(null);
    setRecording(false);
    setAdding(false);
    setSourceError(null);
    setPending([]);
    setIndexing({});
  }, [repo]);
  useEffect(() => {
    void load();
  }, [load, lastEventId]);

  // A job this screen started ends on the bus: it is forgotten, or its failure said.
  const ended = (e: BusEvent) => (e.kind === "job.done" || e.kind === "job.failed") && typeof e.payload.job === "string";
  const settle = useCallback((e: BusEvent, label: string) => {
    const job = e.payload.job as string;
    if (e.kind === "job.failed") setSourceError(`${label} didn't finish: ${(e.payload.error as { message?: string } | undefined)?.message ?? "the engine gave no reason"}`);
    setPending((p) => p.filter((x) => x.job !== job));
    setIndexing((ix) => Object.fromEntries(Object.entries(ix).filter(([, j]) => j !== job)));
  }, []);
  const pendingRef = useRef(pending);
  pendingRef.current = pending;
  const seen = useRef(0);
  useEffect(() => {
    const fresh = recent.filter((e) => e.id > seen.current);
    if (fresh.length === 0) return;
    seen.current = fresh.at(-1)!.id;
    for (const e of fresh.filter(ended)) {
      const mine = pendingRef.current.find((x) => x.job === e.payload.job);
      if (mine) settle(e, mine.label);
    }
  }, [recent, settle]);

  // The documents are searched by the engine, a moment after the typing stops; only the newest answer applies.
  const searched = useRef(0);
  useEffect(() => {
    const q = query.trim();
    if (!repo || !q) {
      searched.current++;
      setHits({ state: "idle" });
      return;
    }
    const mine = ++searched.current;
    setHits({ state: "searching" });
    const timer = setTimeout(() => {
      client.kbSearch(repo, q).then(
        (found) => mine === searched.current && setHits({ state: "found", hits: found }),
        (e: unknown) => mine === searched.current && setHits({ state: "failed", error: message(e) }),
      );
    }, 250);
    return () => clearTimeout(timer);
  }, [client, repo, query]);

  /* A source action on the engine; what it changes is read again as the bus
     reports it. Returns whether the engine took it. */
  const act = async (label: string, run: () => Promise<{ job?: string } | unknown>, follow?: (job: string) => void): Promise<boolean> => {
    if (!repo || busy) return false;
    setBusy(true);
    setSourceError(null);
    try {
      const got = (await run()) as { job?: string };
      if (got && typeof got.job === "string") {
        const job = got.job;
        // The job may have ended before its ack was read (a fast engine): then there is nothing to follow.
        const already = store.getState().recent.find((e) => ended(e) && e.payload.job === job);
        if (already) {
          if (already.kind === "job.failed") settle(already, label);
        } else {
          setPending((p) => [...p, { job, label }]);
          follow?.(job);
        }
      }
      await load();
      return true;
    } catch (e) {
      setSourceError(`${label}: ${message(e)}`);
      return false;
    } finally {
      setBusy(false);
    }
  };
  // The form, with its path, stays up until the engine takes the folder; a refusal shows under it.
  const addFolder = async (path: string, isPrivate: boolean) => {
    if (await act(`Adding ${path}`, () => client.addKbSource(repo!, path, isPrivate))) setAdding(false);
  };
  const sync = (s: KbSource) => void act(`Re-indexing ${sourceName(s)}`, () => client.syncKbSource(repo!, s.id), (job) => setIndexing((ix) => ({ ...ix, [s.id]: job })));
  const remove = (s: KbSource) => void act(`Removing ${sourceName(s)}`, () => client.removeKbSource(repo!, s.id));

  const shown = decisions?.filter((d) => matches(d, query)) ?? [];
  const empty = decisions !== null && sources !== null && decisions.length === 0 && sources.length === 0 && !recording && !adding && !query && pending.length === 0;
  const startRecording = () => {
    setRecording(true);
    setRecorded(null);
  };
  return (
    <section className="flex flex-col gap-s3" aria-label="Knowledge">
      <PageHeader title="Knowledge" lede="What your agents know and remember about this repo: decisions they follow, and documents they can look up.">
        <RepoSelect {...pick} />
      </PageHeader>
      {repos && repos.length === 0 && <p className="text-ink-3">{NO_REPO}</p>}
      {loadError && (
        <div role="alert" className={`${alert} flex items-center justify-between gap-s2`}>
          <span>Could not load knowledge: {loadError}</span>
          <button type="button" className={button} onClick={() => void load()}>
            Retry
          </button>
        </div>
      )}
      {repo && !empty && (
        <input
          type="search"
          className={`${field} w-full py-s1`}
          aria-label="Search knowledge"
          placeholder="Search everything: decisions and documents…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      )}
      {repo && empty && <EmptyState onRecord={startRecording} onAddFolder={() => setAdding(true)} />}
      {repo && hits.state !== "idle" && (
        <section className={`${card} flex flex-col gap-s1 px-s2 py-s2`} aria-label="Document hits" data-testid="hits">
          <header className="flex items-baseline gap-s1">
            <h2 className={h2}>In the documents</h2>
            <span className="text-xs text-ink-3">
              {hits.state === "searching" ? "searching…" : hits.state === "failed" ? "the search failed" : hits.hits.length === 0 ? "nothing matches" : `the best ${hits.hits.length === 1 ? "passage" : `${hits.hits.length} passages`}, as agents get them`}
            </span>
          </header>
          {hits.state === "failed" && (
            <p role="alert" className={alert}>
              Could not search the documents: {hits.error}
            </p>
          )}
          {hits.state === "found" && hits.hits.length === 0 && (
            <p className="text-sm text-ink-2">No indexed document mentions these words. {sources && sources.length === 0 ? "There are no documents to search yet." : "A folder with new files needs re-indexing to pick them up."}</p>
          )}
          {hits.state === "found" && hits.hits.length > 0 && (
            <ul className="flex flex-col divide-y divide-line">
              {hits.hits.map((h) => (
                <Hit key={h.chunk_id} h={h} />
              ))}
            </ul>
          )}
        </section>
      )}
      {repo && !empty && (
        <section className="flex flex-col gap-s1" aria-label="Decisions" data-testid="decisions">
          <header className="flex flex-wrap items-baseline gap-x-s2 gap-y-0.5">
            <h2 className={h2}>Decisions</h2>
            <span className="text-xs text-ink-3" data-testid="decision-count">
              {decisions === null ? "loading…" : query ? `${shown.length} of ${decisions.length} match` : decisions.length === 1 ? "1 in force" : `${decisions.length} in force`}
            </span>
            <span className="flex-1" />
            {!recording && (
              <button type="button" className={main} onClick={startRecording}>
                Record a decision
              </button>
            )}
          </header>
          <p className="text-xs text-ink-3">Rules every agent in this repo follows, with why. A decision on the paths a task touches goes into that task's brief. A new one on a topic already decided replaces the old one only when you say so.</p>
          {recording && (
            <RecordForm
              client={client}
              repo={repo}
              onCancel={() => setRecording(false)}
              onRecorded={(told) => {
                setRecording(false);
                setRecorded(told.length === 0 ? "Recorded." : `Recorded and told ${told.join(", ")}.`);
                void load();
              }}
            />
          )}
          {recorded && (
            <p role="status" className={notice}>
              {recorded}
            </p>
          )}
          {decisions && decisions.length === 0 && !recording && <p className="text-sm text-ink-2">No decisions yet. Record the first one here, or any session can with its record_decision tool.</p>}
          {decisions && decisions.length > 0 && shown.length === 0 && <p className="text-sm text-ink-2">No decision matches.</p>}
          {decisions && shown.length > 0 && (
            <ul className="grid gap-s2 lg:grid-cols-2">
              {shown.map((d) => (
                <DecisionCard key={d.id} d={d} />
              ))}
            </ul>
          )}
        </section>
      )}
      {repo && !empty && (
        <section className="flex flex-col gap-s1" aria-label="Documents" data-testid="sources">
          <header className="flex flex-wrap items-baseline gap-x-s2 gap-y-0.5">
            <h2 className={h2}>Documents</h2>
            <span className="text-xs text-ink-3">{sources === null ? "loading…" : sources.length === 1 ? "1 folder" : `${sources.length} folders`}</span>
            <span className="flex-1" />
            {!adding && (
              <button type="button" className={button} disabled={busy} onClick={() => setAdding(true)}>
                Add folder
              </button>
            )}
          </header>
          <p className="text-xs text-ink-3">Folders of Markdown or text agents can look up. The parts that fit a task go into its brief; the search box above finds them the same way.</p>
          {adding && <AddFolderForm pickFolder={pickFolder} busy={busy} onAdd={(path, isPrivate) => void addFolder(path, isPrivate)} onCancel={() => setAdding(false)} />}
          {pending.map((p) => (
            <p key={p.job} role="status" className={`${notice} flex items-center gap-s1`} data-testid="source-pending">
              <span className="size-3 shrink-0 animate-spin rounded-full border-2 border-pass/55 border-t-transparent" aria-hidden />
              {p.label}…
            </p>
          ))}
          {sourceError && (
            <p role="alert" className={alert} data-testid="source-error">
              {sourceError}
            </p>
          )}
          {sources && sources.length === 0 && !adding && !sourceError && <p className="text-sm text-ink-2">No documents yet. Add a folder of Markdown or text that agents can look up.</p>}
          {sources && sources.length > 0 && (
            <ul className={`${card} flex flex-col divide-y divide-line`}>
              {sources.map((s) => (
                <Source key={s.id} s={s} busy={busy} indexing={s.id in indexing} onSync={() => sync(s)} onRemove={() => remove(s)} />
              ))}
            </ul>
          )}
        </section>
      )}
    </section>
  );
}
