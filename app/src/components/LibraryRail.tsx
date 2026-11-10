/* The Library's navigation, in the app sidebar's place while the Library is open
   (design/library-placement.html §2, design/library-search.html): "← Sessions" back to
   where the person was, then Recent, Pinned, Stage (the stages a design moves through),
   Folders and Sources (the map, imported docs, decisions and kept visuals). The row that
   is open is marked as the Settings rail marks its tab. The glyphs are the approved
   rendering's. Recent, Pinned and Folders have nothing to list until designs, pins and
   folders exist: each says so in a line. */

import { LIBRARY_STAGES, libraryView, type KnowledgeView, type LibraryStage, type Route } from "../routes";

/* What the rail counts: designs waiting on the person, and what each Source holds
   (a row shows no number while it isn't known, or is none). */
export interface LibraryCounts {
  needsYou?: number;
  imported?: number;
  decisions?: number;
  kept?: number;
}

type LibraryRoute = Extract<Route, { screen: "knowledge" }>;

const STAGE_GLYPH: Record<LibraryStage, string> = { "needs-you": "●", "in-review": "○", approved: "✓", building: "▶︎", shipped: "■" };

const SOURCES: { id: Exclude<KnowledgeView, "docs">; label: string; glyph: string; count?: keyof LibraryCounts }[] = [
  { id: "map", label: "Map", glyph: "⌗" },
  { id: "imported", label: "Imported docs", glyph: "⇩", count: "imported" },
  { id: "decisions", label: "Decisions", glyph: "✎", count: "decisions" },
  { id: "kept", label: "Kept visuals", glyph: "▣", count: "kept" },
];

const HEADING = "px-2 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wider text-ink-3";
const NOTHING = "px-2 py-1 text-[12px] text-ink-3";

function Row({ glyph, label, count, mine = false, on, onClick }: { glyph: string; label: string; count?: number; mine?: boolean; on: boolean; onClick(): void }) {
  return (
    <button
      type="button"
      aria-current={on ? "page" : undefined}
      className={`flex w-full items-center gap-2 rounded px-2 py-1 text-left text-[12.5px] hover:bg-surface-2 hover:text-ink ${on ? "bg-surface-2 font-medium text-ink shadow-[inset_2px_0_0_var(--pass)]" : "text-ink-2"}`}
      onClick={onClick}
    >
      <span className="w-3.5 shrink-0 text-center text-[12px] text-ink-3" aria-hidden>
        {glyph}
      </span>
      <span className="min-w-0 flex-1 truncate">{label}</span>{" "}
      {!!count && <span className={`font-mono text-[11.5px] ${mine ? "text-branch" : "text-ink-3"}`}>{count}</span>}
    </button>
  );
}

export function LibraryRail({ route, counts = {}, navigate, onBack }: { route: LibraryRoute; counts?: LibraryCounts; navigate(route: Route): void; onBack(): void }) {
  const view = libraryView(route);
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-auto" data-testid="library-rail">
      <button type="button" onClick={onBack} className="rounded px-2 py-1 text-left text-[12.5px] text-ink-2 hover:bg-surface-2 hover:text-ink">
        ← Sessions
      </button>
      <div className="px-2 pt-1 text-[13px] font-semibold text-ink">Library</div>
      <nav aria-label="Library" className="flex flex-col gap-px">
        <div className={HEADING}>Recent</div>
        <p className={NOTHING}>Nothing opened yet.</p>
        <div className={HEADING}>Pinned</div>
        <p className={NOTHING}>Nothing pinned yet.</p>
        <div className={HEADING}>Stage</div>
        {LIBRARY_STAGES.map((stage) => (
          <Row
            key={stage.id}
            glyph={STAGE_GLYPH[stage.id]}
            label={stage.label}
            count={stage.id === "needs-you" ? counts.needsYou : undefined}
            mine={stage.id === "needs-you"}
            on={view === "docs" && route.stage === stage.id}
            onClick={() => navigate({ screen: "knowledge", view: "docs", stage: stage.id })}
          />
        ))}
        <div className={HEADING}>Folders</div>
        <p className={NOTHING}>No folders yet.</p>
        <div className={HEADING}>Sources</div>
        {SOURCES.map((source) => (
          <Row key={source.id} glyph={source.glyph} label={source.label} count={source.count && counts[source.count]} on={view === source.id} onClick={() => navigate({ screen: "knowledge", view: source.id })} />
        ))}
      </nav>
    </div>
  );
}
