/* The Library's navigation, in the app sidebar's place while the Library is open:
   "← Sessions" back to
   where the person was, then Recent, Pinned, Stage (the stages a design moves through),
   Folders and Sources (the map, imported docs, decisions and kept visuals). The row that
   is open is marked as the Settings rail marks its tab. The glyphs are the approved
   rendering's.

   Only what is real shows. Today that is Stage › Needs you (the plans waiting in the
   Inbox), the Folders the server lists, and the Sources. Recent, Pinned and the other
   stages render nothing until they are given (`recent`, `pinned`, a stage's count):
   the doc engine turns them on by passing them. */

import { useRef, useState, type FormEvent } from "react";
import { useReaderDoc } from "../store/libraryReader";
import type { DocScope, FolderGroup, FolderVisibility } from "../api/client";
import { LIBRARY_STAGES, libraryView, type KnowledgeView, type LibraryStage, type Route } from "../routes";

/* What the rail counts: designs at each stage ("needs-you": waiting on the person; a
   stage with no count given isn't listed, but Needs you always is), and what each Source
   holds (a row shows no number while it isn't known, or is none). */
export interface LibraryCounts {
  stages?: Partial<Record<LibraryStage, number>>;
  imported?: number;
  decisions?: number;
  kept?: number;
}

/* A design the rail lists by name (Recent, Pinned): its library doc id and title. */
export interface LibraryEntry {
  id: string;
  title: string;
}

type LibraryRoute = Extract<Route, { screen: "knowledge" }>;

const STAGE_GLYPH: Record<LibraryStage, string> = { "needs-you": "●", "in-review": "○", approved: "✓", building: "▶︎", shipped: "■" };

const SOURCES: { id: Exclude<KnowledgeView, "docs">; label: string; glyph: string; count?: "imported" | "decisions" | "kept" }[] = [
  { id: "map", label: "Map", glyph: "⌗" },
  { id: "imported", label: "Imported docs", glyph: "⇩", count: "imported" },
  { id: "decisions", label: "Decisions", glyph: "✎", count: "decisions" },
  { id: "kept", label: "Kept visuals", glyph: "▣", count: "kept" },
];

const HEADING = "px-2 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wider text-ink-3";

function Row({ glyph, pin = false, label, count, mine = false, on, onClick }: { glyph: string; pin?: boolean; label: string; count?: number; mine?: boolean; on: boolean; onClick(): void }) {
  return (
    <button
      type="button"
      aria-current={on ? "page" : undefined}
      className={`flex w-full items-center gap-2 rounded px-2 py-1 text-left text-[12.5px] hover:bg-surface-2 hover:text-ink ${on ? "bg-surface-2 font-medium text-ink shadow-[inset_2px_0_0_var(--pass)]" : "text-ink-2"}`}
      onClick={onClick}
    >
      <span className={`w-3.5 shrink-0 text-center text-[12px] ${pin ? "text-branch" : "text-ink-3"}`} aria-hidden>
        {glyph}
      </span>
      <span className="min-w-0 flex-1 truncate">{label}</span>{" "}
      {!!count && <span className={`font-mono text-[11.5px] ${mine ? "text-branch" : "text-ink-3"}`}>{count}</span>}
    </button>
  );
}

/* The scope in the daemon's spelling (user, team:<id>, org:<id>) and the heading the rail gives it. */
const scopeSpec = (scope: DocScope) => (scope.kind === "user" ? "user" : `${scope.kind === "group" ? "team" : "org"}:${scope.id}`);
const scopeName = (scope: DocScope) => (scope.kind === "user" ? "My folders" : scope.name || (scope.kind === "group" ? "Team" : "Org"));
const VISIBLE_TO = (scope: DocScope, visibility: FolderVisibility) => (visibility === "private" ? "only me" : scope.kind === "group" ? "team" : scope.kind === "org" ? "org" : "shared");

/* The Folders group: My folders, then each team and the org,
   each folder saying who can see it. "+ New" opens the New folder form; while a design is open, each
   folder offers "Move here". Nothing is listed until `folders` arrives. */
type CreateFolder = (scope: string, name: string, visibility: FolderVisibility, canAdd: "owner" | "members") => Promise<void> | void;

function Folders({ folders, doc, onCreate, onMove }: { folders: FolderGroup[]; doc?: string; onCreate?: CreateFolder; onMove?(doc: string, scope: string, folder: string): Promise<void> | void }) {
  const [adding, setAdding] = useState(false);
  const [canAdd, setCanAdd] = useState<"owner" | "members">("owner");
  const [pending, setPending] = useState(false);
  const sending = useRef(false);     // set at once: a second click or Enter before the first renders is ignored
  const [name, setName] = useState("");
  const [where, setWhere] = useState("user");
  const [visibility, setVisibility] = useState<FolderVisibility>("private");
  const [error, setError] = useState<string | null>(null);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim() || sending.current) return;
    sending.current = true;
    setPending(true);
    try {
      await onCreate?.(where, name.trim(), visibility, canAdd);
      setAdding(false);
      setName("");
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't create the folder");
    } finally {
      sending.current = false;
      setPending(false);
    }
  };
  const move = async (folder: string, scope: string) => {
    try {
      await onMove?.(doc!, scope, folder);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't move the design");
    }
  };
  return (
    <>
      <div className={`${HEADING} flex items-center justify-between`}>
        Folders
        {onCreate && folders.length > 0 && (
          <button type="button" className="normal-case tracking-normal text-branch hover:underline" onClick={() => setAdding(!adding)}>
            + New
          </button>
        )}
      </div>
      {adding && (
        <form aria-label="New folder" className="flex flex-col gap-1 px-2 py-1 text-[12px]" onSubmit={submit}>
          <input aria-label="Folder name" className="rounded border border-line bg-surface px-1.5 py-1 text-ink" value={name} onChange={(e) => setName(e.target.value)} />
          <select aria-label="Folder in" className="rounded border border-line bg-surface px-1 py-1 text-ink" value={where} onChange={(e) => setWhere(e.target.value)}>
            {folders.map(({ scope }) => (
              <option key={scopeSpec(scope)} value={scopeSpec(scope)}>
                {scopeName(scope)}
              </option>
            ))}
          </select>
          <select aria-label="Who can see it" className="rounded border border-line bg-surface px-1 py-1 text-ink" value={visibility} onChange={(e) => setVisibility(e.target.value as FolderVisibility)}>
            <option value="private">Only me</option>
            <option value="shared">Everyone in the scope</option>
          </select>
          <select aria-label="Who can add" className="rounded border border-line bg-surface px-1 py-1 text-ink" value={canAdd} onChange={(e) => setCanAdd(e.target.value as "owner" | "members")}>
            <option value="owner">Only me</option>
            <option value="members">Anyone who can see it</option>
          </select>
          <button type="submit" disabled={pending} className="self-start rounded bg-surface-2 px-2 py-1 font-medium text-ink hover:bg-line disabled:opacity-50">
            Create folder
          </button>
        </form>
      )}
      {error && (
        <p role="alert" className="px-2 py-1 text-[12px] text-fail">
          {error}
        </p>
      )}
      {folders.map(({ scope, folders: list }) => (
        <div key={scopeSpec(scope)} className="flex flex-col gap-px" data-testid={`folders-${scopeSpec(scope)}`}>
          <div className="flex items-center gap-2 px-2 py-1 text-[12.5px] text-ink-2">
            <span className="w-3.5 shrink-0 text-center text-[12px] text-ink-3" aria-hidden>
              ▾
            </span>
            {scopeName(scope)}
          </div>
          {list.map((folder) => (
            <div key={folder.id} className="group flex items-center gap-2 rounded py-1 pl-7 pr-2 text-[12.5px] text-ink-2 hover:bg-surface-2">
              <span className="min-w-0 flex-1 truncate">{folder.name}</span>
              <span className="text-[11px] text-ink-3">{VISIBLE_TO(scope, folder.visibility)}</span>
              {doc && onMove && (
                <button type="button" aria-label={`Move here: ${folder.name}`} className="text-[11px] text-branch hover:underline" onClick={() => move(folder.id, scopeSpec(scope))}>
                  Move here
                </button>
              )}
            </div>
          ))}
        </div>
      ))}
    </>
  );
}

export function LibraryRail({ route, counts = {}, recent = [], pinned = [], folders = [], navigate, onBack, onCreateFolder, onMoveDoc }: { route: LibraryRoute; counts?: LibraryCounts; recent?: LibraryEntry[]; pinned?: LibraryEntry[]; folders?: FolderGroup[]; navigate(route: Route): void; onBack(): void; onCreateFolder?: CreateFolder; onMoveDoc?(doc: string, scope: string, folder: string): Promise<void> | void }) {
  const view = libraryView(route);
  const readerDoc = useReaderDoc();     // the design open in the Docs reader (not route.doc: opening a row never sets it)
  const stages = LIBRARY_STAGES.filter((stage) => stage.id === "needs-you" || counts.stages?.[stage.id] != null);
  const entries = (list: LibraryEntry[], glyph: string, pin = false) =>
    list.map((entry) => <Row key={entry.id} glyph={glyph} pin={pin} label={entry.title} on={view === "docs" && route.doc === entry.id} onClick={() => navigate({ screen: "knowledge", view: "docs", doc: entry.id })} />);
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-auto" data-testid="library-rail">
      <button type="button" onClick={onBack} className="rounded px-2 py-1 text-left text-[12.5px] text-ink-2 hover:bg-surface-2 hover:text-ink">
        ← Sessions
      </button>
      <div className="px-2 pt-1 text-[13px] font-semibold text-ink">Library</div>
      <nav aria-label="Library" className="flex flex-col gap-px">
        {recent.length > 0 && <div className={HEADING}>Recent</div>}
        {entries(recent, "◷")}
        {pinned.length > 0 && <div className={HEADING}>Pinned</div>}
        {entries(pinned, "◆", true)}
        <div className={HEADING}>Stage</div>
        {stages.map((stage) => (
          <Row
            key={stage.id}
            glyph={STAGE_GLYPH[stage.id]}
            label={stage.label}
            count={counts.stages?.[stage.id]}
            mine={stage.id === "needs-you"}
            on={view === "docs" && route.stage === stage.id}
            onClick={() => navigate({ screen: "knowledge", view: "docs", stage: stage.id })}
          />
        ))}
        <Folders folders={folders} doc={readerDoc ?? undefined} onCreate={onCreateFolder} onMove={onMoveDoc} />
        <div className={HEADING}>Sources</div>
        {SOURCES.map((source) => (
          <Row key={source.id} glyph={source.glyph} label={source.label} count={source.count && counts[source.count]} on={view === source.id} onClick={() => navigate({ screen: "knowledge", view: source.id })} />
        ))}
      </nav>
    </div>
  );
}
