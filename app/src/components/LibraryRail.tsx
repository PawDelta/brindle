/* The Library's navigation, in the app sidebar's place while the Library is open: "← Sessions"
   back to where the person was, then All designs, Needs you (what waits on the person, counted in
   its own colour) and the five stages a design reaches; the Folders, grouped by the repo each was
   made for (a repo row opens all its folders' designs, a folder row its own; a lock marks a folder
   only its creator sees), newest first; and the Sources (the map, imported docs, decisions and
   kept visuals). The row that is open is marked as the Settings rail marks its tab. The glyphs
   are the approved rendering's (library-redesign option A). There is no repo dropdown: the
   Library reads every repo at once.

   While a design is open in the reader, each folder offers "Move here". "+ New" opens the New
   folder form: its name, the repo it is for, where it lives and who sees and adds to it. */

import { useMemo, useRef, useState, type FormEvent } from "react";
import { useReaderDoc } from "../store/libraryReader";
import type { AnalysisDoc, FolderGroup, FolderVisibility } from "../api/client";
import type { RepoDTO } from "../api/schema";
import { LIBRARY_STAGES, libraryView, type KnowledgeView, type LibraryStage, type Route } from "../routes";
import { folderTree, placedFolders, type DesignRecord } from "../screens/Library/libraryStages";
import { LockIcon } from "./icons";

/* What the rail counts: designs at each stage ("needs-you": waiting on the person), all of them,
   and what each Source holds (a row shows no number while it isn't known, or is none). */
export interface LibraryCounts {
  all?: number;
  stages?: Partial<Record<LibraryStage, number>>;
  imported?: number;
  decisions?: number;
  kept?: number;
}

type LibraryRoute = Extract<Route, { screen: "knowledge" }>;

const STAGE_GLYPH: Record<LibraryStage, string> = { "needs-you": "●", drafting: "✎", "in-review": "○", approved: "✓", building: "▶︎", shipped: "■" };

const SOURCES: { id: Exclude<KnowledgeView, "docs">; label: string; glyph: string; count?: "imported" | "decisions" | "kept" }[] = [
  { id: "map", label: "Map", glyph: "⌗" },
  { id: "imported", label: "Imported docs", glyph: "⇩", count: "imported" },
  { id: "decisions", label: "Decisions", glyph: "✎", count: "decisions" },
  { id: "kept", label: "Kept visuals", glyph: "▣", count: "kept" },
];

const HEADING = "px-2 pb-1 pt-2.5 text-[10px] font-semibold uppercase tracking-wider text-ink-3";
const FIELD = "rounded border border-line bg-surface px-1.5 py-1 text-ink";

function Row({ glyph, label, count, mine = false, on, inset = false, locked = false, title, onClick }: { glyph?: string; label: string; count?: number; mine?: boolean; on: boolean; inset?: boolean; locked?: boolean; title?: string; onClick(): void }) {
  return (
    <button
      type="button"
      aria-current={on ? "page" : undefined}
      title={title}
      className={`flex w-full items-center gap-2 rounded py-1 pr-2 text-left text-[12.5px] hover:bg-surface-2 hover:text-ink ${inset ? "pl-[30px]" : "pl-2"} ${on ? "bg-surface-2 font-medium text-ink shadow-[inset_2px_0_0_var(--pass)]" : "text-ink-2"}`}
      onClick={onClick}
    >
      {glyph !== undefined && (
        <span className={`w-3.5 shrink-0 text-center text-[12px] ${mine ? "text-sap" : "text-ink-3"}`} aria-hidden>
          {glyph}
        </span>
      )}
      <span className="min-w-0 truncate">{label}</span>
      {locked && <LockIcon size={13} className="shrink-0 text-ink-3" />}
      <span className="flex-1" />
      {!!count && <span className={`font-mono text-[11.5px] ${mine ? "text-sap" : "text-ink-3"}`}>{count}</span>}
    </button>
  );
}

/* The scope in the daemon's spelling (user, team:<id>, org:<id>) and the name the form gives it. */
const scopeSpec = (scope: FolderGroup["scope"]) => (scope.kind === "user" ? "user" : `${scope.kind === "group" ? "team" : "org"}:${scope.id}`);
const scopeName = (scope: FolderGroup["scope"]) => (scope.kind === "user" ? "Just me" : scope.name || (scope.kind === "group" ? "Team" : "Organization"));

type CreateFolder = (scope: string, name: string, visibility: FolderVisibility, canAdd: "owner" | "members", repo?: string) => Promise<void> | void;
type Repos = Pick<RepoDTO, "name" | "repo_root">[];

function NewFolder({ folders, repos, defaultRepo, onCreate, onDone }: { folders: FolderGroup[]; repos: Repos; defaultRepo?: string; onCreate: CreateFolder; onDone(): void }) {
  const [canAdd, setCanAdd] = useState<"owner" | "members">("owner");
  const [pending, setPending] = useState(false);
  const sending = useRef(false);     // set at once: a second click or Enter before the first renders is ignored
  const [name, setName] = useState("");
  const [repo, setRepo] = useState(defaultRepo ?? repos[0]?.repo_root ?? "");
  const [where, setWhere] = useState("user");
  const [visibility, setVisibility] = useState<FolderVisibility>("private");
  const [error, setError] = useState<string | null>(null);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim() || sending.current) return;
    sending.current = true;
    setPending(true);
    try {
      await onCreate(where, name.trim(), visibility, canAdd, repo || undefined);
      setError(null);
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't create the folder");
    } finally {
      sending.current = false;
      setPending(false);
    }
  };
  const labelled = "flex flex-col gap-0.5 text-[11px] text-ink-3";
  return (
    <form aria-label="New folder" className="flex flex-col gap-1.5 px-2 py-1.5 text-[12px]" onSubmit={submit}>
      <label className={labelled}>Name<input aria-label="Folder name" className={FIELD} value={name} onChange={(e) => setName(e.target.value)} /></label>
      {repos.length > 0 && (
        <label className={labelled}>
          Repo
          <select aria-label="Repo" className={FIELD} value={repo} onChange={(e) => setRepo(e.target.value)}>
            {repos.map((r) => <option key={r.repo_root} value={r.repo_root}>{r.name}</option>)}
          </select>
        </label>
      )}
      <label className={labelled}>
        Belongs to
        <select aria-label="Folder in" className={FIELD} value={where} onChange={(e) => setWhere(e.target.value)}>
          {folders.map(({ scope }) => <option key={scopeSpec(scope)} value={scopeSpec(scope)}>{scopeName(scope)}</option>)}
        </select>
      </label>
      <label className={labelled}>
        Who can see it
        <select aria-label="Who can see it" className={FIELD} value={visibility} onChange={(e) => setVisibility(e.target.value as FolderVisibility)}>
          <option value="private">Only me</option>
          <option value="shared">Everyone it belongs to</option>
        </select>
      </label>
      <label className={labelled}>
        Who can add designs
        <select aria-label="Who can add" className={FIELD} value={canAdd} onChange={(e) => setCanAdd(e.target.value as "owner" | "members")}>
          <option value="owner">Only me</option>
          <option value="members">Anyone who can see it</option>
        </select>
      </label>
      {error && <p role="alert" className="text-fail">{error}</p>}
      <button type="submit" disabled={pending} className="side-btn self-start">Create folder</button>
    </form>
  );
}

export interface LibraryRailProps {
  route: LibraryRoute;
  counts?: LibraryCounts;
  folders?: FolderGroup[];
  /* The designs and what was read of each, for the folders' counts and order. */
  docs?: AnalysisDoc[];
  records?: Map<string, DesignRecord>;
  repos?: Repos | null;
  navigate(route: Route): void;
  onBack(): void;
  onCreateFolder?: CreateFolder;
  onMoveDoc?(doc: string, scope: string, folder: string): Promise<void> | void;
}

const NO_DOCS: AnalysisDoc[] = [];
const NO_RECORDS = new Map<string, DesignRecord>();

export function LibraryRail({ route, counts = {}, folders = [], docs = NO_DOCS, records = NO_RECORDS, repos, navigate, onBack, onCreateFolder, onMoveDoc }: LibraryRailProps) {
  const view = libraryView(route);
  const readerDoc = useReaderDoc();     // the design open in the Docs reader (not route.doc: opening a row never sets it)
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const tree = useMemo(() => folderTree(placedFolders(folders), docs, records, repos), [folders, docs, records, repos]);
  const designs = view === "docs";
  const picked = designs && !route.stage && !route.folder && !route.repo;
  // The switch to List or Board keeps the folder picked: a row's route carries only what it picks.
  const go = (pick: Pick<LibraryRoute, "stage" | "folder" | "repo">) => navigate({ screen: "knowledge", view: "docs", ...pick });
  const move = async (folder: string, scope: string) => {
    try {
      await onMoveDoc?.(readerDoc!, scope, folder);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't move the design");
    }
  };
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-auto" data-testid="library-rail">
      <button type="button" onClick={onBack} className="self-start rounded px-2 py-1 text-left text-[12.5px] text-ink-2 hover:bg-surface-2 hover:text-ink">
        ← Sessions
      </button>
      <nav aria-label="Library" className="flex flex-col gap-px">
        <Row glyph="▤" label="All designs" count={counts.all} on={picked} onClick={() => go({})} />
        {LIBRARY_STAGES.map((stage) => (
          <Row key={stage.id} glyph={STAGE_GLYPH[stage.id]} label={stage.label} count={counts.stages?.[stage.id]} mine={stage.id === "needs-you"} on={designs && route.stage === stage.id} onClick={() => go({ stage: stage.id })} />
        ))}
        <div className={`${HEADING} flex items-center justify-between`}>
          Folders
          {onCreateFolder && folders.length > 0 && (
            <button type="button" className="text-[11px] font-medium normal-case tracking-normal text-branch hover:underline" onClick={() => setAdding(!adding)}>
              + New
            </button>
          )}
        </div>
        {adding && onCreateFolder && <NewFolder folders={folders} repos={repos ?? []} defaultRepo={route.repo} onCreate={onCreateFolder} onDone={() => setAdding(false)} />}
        {error && <p role="alert" className="px-2 py-1 text-[12px] text-fail">{error}</p>}
        {tree.map((t) => (
          <div key={t.key} className="flex flex-col gap-px" data-testid={`folders-${t.key}`}>
            {t.repo ? (
              <Row glyph="▾" label={t.label} count={t.count} title="This repo's folders: open to see all its designs" on={designs && route.repo === t.repo && !route.folder} onClick={() => go({ repo: t.repo! })} />
            ) : (
              <div className="flex items-center gap-2 px-2 py-1 text-[12.5px] text-ink-2">
                <span className="w-3.5 shrink-0 text-center text-[12px] text-ink-3" aria-hidden>▾</span>
                {t.label}
              </div>
            )}
            {t.folders.map((f) => (
              <div key={f.key} className="group flex items-center">
                <div className="min-w-0 flex-1">
                  <Row inset label={f.name} count={f.count} locked={f.private} title={f.private ? "Only you see this folder" : undefined} on={designs && route.folder === f.key} onClick={() => go({ folder: f.key, repo: f.repo ?? undefined })} />
                </div>
                {readerDoc && onMoveDoc && (
                  <button type="button" aria-label={`Move here: ${f.name}`} className="shrink-0 px-1 text-[11px] text-branch hover:underline" onClick={() => void move(f.id, f.spec)}>
                    Move here
                  </button>
                )}
              </div>
            ))}
          </div>
        ))}
        <div className={HEADING}>Sources</div>
        {SOURCES.map((source) => (
          <Row key={source.id} glyph={source.glyph} label={source.label} count={source.count && counts[source.count]} on={view === source.id} onClick={() => navigate({ screen: "knowledge", view: source.id })} />
        ))}
      </nav>
    </div>
  );
}
