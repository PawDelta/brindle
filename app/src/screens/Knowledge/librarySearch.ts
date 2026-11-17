import { useEffect, useState } from "react";
import type { AnalysisDoc, AnalysisDocDetail, DaemonClient, KeptVisualDTO } from "../../api/client";

export type LibraryType = "all" | "designs" | "decisions" | "imported" | "kept";
export type SearchClient = Partial<Pick<DaemonClient, "decisions" | "kbSearch" | "keptVisuals">>;
export interface LibraryHit {
  id: string;
  title: string;
  type: Exclude<LibraryType, "all" | "designs">;
  text: string;
  changed?: number;
  author?: string;
  kept?: KeptVisualDTO;
}

export function docMatch(d: AnalysisDoc, detail: AnalysisDocDetail | undefined, query: string): string | null {
  if (!query) return "";
  const places: [string, string | undefined][] = [
    ["Title", d.title], ["Summary", d.summary], ["Topics", d.topics?.join(" ")],
    ...(detail?.files ?? []).map((f): [string, string] => ["Document", f.md]),
    ...(detail?.threads ?? []).flatMap((t): [string, string][] => [["Comment", t.text], ...t.replies.map((r): [string, string] => ["Comment", r.text])]),
    ...(detail?.decisions ?? []).map((d): [string, string] => ["Decision", [d.title, ...d.options.map((o) => o.label)].join(" ")]),
  ];
  for (const [where, text] of places) {
    const at = text?.toLowerCase().indexOf(query) ?? -1;
    if (at >= 0) return `${where} · ${text!.slice(Math.max(0, at - 45), at + query.length + 100)}`;
  }
  return null;
}

/* The existing routes provide full docs and repo-local sources. Never apply a
   team/scope filter to a source whose API cannot establish that membership. */
export function useLibrarySearch(client: SearchClient & Pick<DaemonClient, "doc">, docs: AnalysisDoc[], repo: string | undefined, query: string, whole: boolean) {
  const [details, setDetails] = useState<{ docs: AnalysisDoc[]; values: Map<string, AnalysisDocDetail> }>();
  const [sources, setSources] = useState<{ query: string; repo?: string; hits: LibraryHit[]; error: boolean }>();
  const [failedDocs, setFailedDocs] = useState<AnalysisDoc[]>();
  useEffect(() => {
    if (!query || !docs.length) return;
    let live = true;
    Promise.allSettled(docs.map((d) => client.doc(d.id, repo))).then((got) => {
      if (!live) return;
      setDetails({ docs, values: new Map(got.flatMap((r, i) => r.status === "fulfilled" ? [[docs[i].id, r.value] as const] : [])) });
      setFailedDocs(got.some((r) => r.status === "rejected") ? docs : undefined);
    });
    return () => { live = false; };
    // Fetch the corpus once, rather than on every keystroke.
  }, [client, docs, repo, Boolean(query)]);
  useEffect(() => {
    if (!whole || !repo) return;
    let live = true;
    Promise.allSettled([
      client.decisions?.(repo), query ? client.kbSearch?.(repo, query) : undefined, client.keptVisuals?.(),
    ]).then(([decisions, imported, kept]) => {
      if (!live) return;
      const hits: LibraryHit[] = [];
      if (decisions.status === "fulfilled" && Array.isArray(decisions.value)) {
        for (const d of decisions.value) {
          const text = `${d.topic}\n${d.body}\n${d.rationale ?? ""}`;
          if (text.toLowerCase().includes(query)) hits.push({ id: `decision:${d.id}`, title: d.topic, type: "decisions", text, changed: d.created_at });
        }
      }
      if (imported.status === "fulfilled" && Array.isArray(imported.value)) {
        const seen = new Set<string>();
        for (const d of imported.value) {
          // Relative paths are shared across sources. Without a URL the
          // chunk id preserves distinct results rather than guessing a doc.
          const identity = JSON.stringify(d.url ? ["url", d.url] : ["chunk", d.chunk_id]);
          if (seen.has(identity)) continue;
          seen.add(identity);
          hits.push({ id: `imported:${d.chunk_id}`, title: d.title || d.external_id, type: "imported", text: d.text, changed: d.updated_at ?? undefined });
        }
      }
      if (kept.status === "fulfilled" && Array.isArray(kept.value)) {
        for (const d of kept.value) {
          const text = `${d.title}\n${d.answer?.option ?? ""}\n${d.answer?.note ?? ""}`;
          if (d.repo === repo && text.toLowerCase().includes(query)) hits.push({ id: `kept:${d.id}`, title: d.title, type: "kept", text, changed: d.kept_at, kept: d });
        }
      }
      setSources({ query, repo, hits, error: [decisions, imported, kept].some((r) => r.status === "rejected") });
    });
    return () => { live = false; };
  }, [client, repo, query, whole]);
  return {
    details: details?.docs === docs ? details.values : undefined,
    hits: whole && sources?.query === query && sources.repo === repo ? sources.hits : [],
    searching: (!!query && docs.length > 0 && details?.docs !== docs) || (whole && !!repo && (sources?.query !== query || sources.repo !== repo)),
    incomplete: failedDocs === docs || (whole && sources?.query === query && sources.repo === repo && sources.error),
  };
}
