import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import type { DaemonClient, DocScope, KbSource, KnowledgeMap } from "../../api/client";
import type { DecisionDTO } from "../../api/schema";
import { alert, button, card, field, h2 } from "../../components/ui";
import { Reader, scopeName } from "./DocsView";
import { DecisionCard } from "./KnowledgeScreen";
import { docGraph } from "./graphLayout";
import { joinLocal, MapGraph, type GraphNode } from "./MapGraph";

interface MapViewProps {
  client: DaemonClient;
  repo?: string;
  decisions: DecisionDTO[];
  sources: KbSource[];
}

const message = (e: unknown) => e instanceof Error ? e.message : String(e);
type MapAreaFilter = { paths: string[]; scope: DocScope };

export function MapView(props: MapViewProps) {
  return <MapArea key={props.repo} {...props} />;
}

/* The first line of prose in a doc's markdown, as a stand-in summary. */
const firstLine = (md = "") => md.split("\n").map((l) => l.trim()).find((l) => l && !l.startsWith("#"))?.slice(0, 240) ?? "";

function MapArea({ client, repo, decisions, sources }: MapViewProps) {
  const [scopes, setScopes] = useState<DocScope[]>([]);
  const [scopeKey, setScopeKey] = useState("");
  const [input, setInput] = useState("");
  const [area, setArea] = useState<MapAreaFilter | null>(null);
  const [result, setResult] = useState<{ request: MapAreaFilter; map: KnowledgeMap } | null>(null);
  // A result belongs to the exact applied filter, including while effects settle.
  const map = result?.request === area ? result?.map ?? null : null;
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<GraphNode | null>(null);
  const [doc, setDoc] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ node: string; summary: string; text?: string } | null>(null);
  const [local, setLocal] = useState<{ id: string; title: string; text: string } | null>(null);
  const selected = useRef<{ node: string; gen: number } | null>(null);
  const generation = useRef(0);
  useEffect(() => {
    let active = true;
    selected.current = null; generation.current++; setLocal(null); setPreview(null);
    setScopes([]); setScopeKey(""); setArea(null); setResult(null); setOpen(null); setDoc(null); setError(null);
    if (repo) client.docScopes(repo).then((got) => {
      if (active) {
        setScopes(got.scopes); setScopeKey(got.scopes[0] ? `${got.scopes[0].kind}:${got.scopes[0].id}` : "");
        if (got.scopes[0]) setArea({ scope: got.scopes[0], paths: [] });
      }
    }, (e: unknown) => { if (active) setError(message(e)); });
    return () => { active = false; };
  }, [client, repo]);
  useEffect(() => {
    let active = true;
    const request = area;
    selected.current = null; generation.current++; setLocal(null); setPreview(null);
    setResult(null); setOpen(null); setDoc(null); setError(null);
    if (repo && request) client.knowledgeMap(repo, request.scope, request.paths).then(
      (got) => { if (active) setResult({ request, map: got }); },
      (e: unknown) => { if (active) setError(message(e)); },
    );
    return () => { active = false; };
  }, [client, repo, area]);
  const graph = useMemo(
    () => map && repo && area ? joinLocal(map, decisions, sources, repo, area) : null,
    [map, decisions, sources, repo, area],
  );
  // What the graph shows: documents and the local records that reach them.
  const shown = useMemo(() => graph && docGraph(graph.nodes, graph.edges), [graph]);
  const titles = useMemo(() => new Map((map?.nodes ?? []).filter((n) => n.kind === "doc").map((n) => [n.doc_id ?? n.id, n.title ?? n.id])), [map]);
  const learn = useCallback(() => {}, []);
  const close = () => setDoc(null);
  const changeScope = (key: string) => {
    const scope = scopes.find((s) => `${s.kind}:${s.id}` === key);
    setScopeKey(key);
    // Keep the applied paths; unsubmitted path edits stay in the input.
    if (scope) setArea((current) => {
      if (!current) return null;
      const sameScope = current.scope.kind === scope.kind && current.scope.id === scope.id;
      return sameScope ? current : { ...current, scope };
    });
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const scope = scopes.find((s) => `${s.kind}:${s.id}` === scopeKey);
    const paths = [...new Set(input.split(/[,\n]/).map((p) => p.trim()).filter(Boolean))];
    if (scope) setArea((current) => {
      const pending = !map && !error;
      const same = current?.scope.kind === scope.kind && current.scope.id === scope.id && JSON.stringify(current.paths) === JSON.stringify(paths);
      return pending && same ? current : { scope, paths };
    });
  };
  /* A click only previews: the sidebar shows the node, and the doc link opens
     the in-app reader. Nothing here reaches the OS. */
  const select = (node: GraphNode) => {
    setOpen(node);
    setDoc(null);
    setLocal(null);
    // Activating the node already selected (a double-click) reuses its preview and in-flight fetch.
    if (selected.current?.node === node.id) return;
    const gen = ++generation.current;
    // The selection is captured now: a response from an older generation is dropped.
    selected.current = { node: node.id, gen };
    setPreview(null);
    const current = () => generation.current === gen;
    if (node.kind === "decision") setPreview({ node: node.id, summary: firstLine(node.decision.body) });
    else if (node.kind === "imported") setPreview({ node: node.id, summary: `Imported folder${node.source.docs !== undefined ? ` · ${node.source.docs} documents` : ""} · ${node.path}` });
    else if (node.kind === "doc" && repo) {
      if (node.local_path) {
        // The engine's own document id, from the node's `local:kb:<id>`; no search involved.
        client.kbDoc(repo, node.id.replace(/^local:kb:/, "")).then((got) => {
          if (current() && got.text) setPreview({ node: node.id, summary: firstLine(got.text), text: got.text });
        }, () => {});
      } else client.doc(node.doc_id ?? node.id, repo).then(
        (got) => { if (current()) setPreview({ node: node.id, summary: got.summary || firstLine(got.files[0]?.md) }); },
        () => {},
      );
    }
    if (node.kind === "path" && node.path && area) { setInput(node.path); setArea({ scope: area.scope, paths: [node.path] }); }
  };
  /* The sidebar was closed: forget the selection so an old fetch can't land and a re-click starts afresh. */
  const deselect = useCallback(() => { selected.current = null; generation.current++; setPreview(null); setOpen(null); }, []);
  const viewable = open?.kind === "doc" && !open.local_path ? open.doc_id ?? open.id : null;
  const onDoc = viewable ? () => setDoc(viewable) : open?.kind === "doc" && preview?.node === open.id && preview.text ? () => setLocal({ id: open.id, title: open.title ?? open.id, text: preview.text! }) : undefined;
  return <section className="flex flex-col gap-s2" aria-label="Map" data-testid="map">
    <header><h2 className={h2}>Library map</h2><p className="text-sm text-ink-2">All knowledge in this library scope, including local decisions and imported folders in Mine. Code paths optionally narrow the map. Shared paths and topics connect records.</p></header>
    {!repo ? <p className="text-ink-3">Choose a repo to explore its knowledge.</p> : <form onSubmit={submit} className={`${card} flex flex-wrap items-end gap-s1 p-s2`}>
      <label className="flex flex-col gap-1 text-xs text-ink-2">Library scope<select className={field} value={scopeKey} onChange={(e) => changeScope(e.target.value)}>{scopes.map((s) => <option key={`${s.kind}:${s.id}`} value={`${s.kind}:${s.id}`}>{scopeName(s)}</option>)}</select></label>
      <label className="flex min-w-48 flex-1 flex-col gap-1 text-xs text-ink-2">Code paths<input className={field} value={input} onChange={(e) => setInput(e.target.value)} placeholder="src/cache, src/db" /></label>
      <button type="submit" className={button} disabled={!scopeKey}>Explore area</button>
    </form>}
    {error && <p role="alert" className={alert}>{error}</p>}
    {repo && !area && <p className="text-sm text-ink-3">Choose a library scope to load its map.</p>}
    {area && !map && !error && <p role="status">Loading neighbourhood…</p>}
    {graph && shown && <><p className="text-xs text-ink-3">{shown.nodes.length} documents and local records · {shown.edges.length} links · drag nodes or the background, scroll to zoom, click a node for details</p>
      {shown.nodes.length ? <MapGraph {...graph} onOpen={select} onDeselect={deselect} preview={preview} onDoc={onDoc} /> : <p>No knowledge in this map yet.</p>}
    </>}
    {open?.kind === "decision" && <ul><DecisionCard d={open.decision} /></ul>}
    {local && <Reader key={local.id} client={client} repo={repo} id={local.id} local={{ title: local.title, markdown: local.text }} targets={scopes} titles={titles} onBack={() => setLocal(null)} onDeleted={() => setLocal(null)} onOpen={setDoc} onScopes={learn} />}
    {doc && <Reader key={doc} client={client} repo={repo} id={doc} targets={scopes} titles={titles} onBack={close} onDeleted={close} onOpen={setDoc} onScopes={learn} />}
  </section>;
}
