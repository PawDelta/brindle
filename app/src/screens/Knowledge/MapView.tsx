import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import type { DaemonClient, DocScope, KbSource, KnowledgeMap } from "../../api/client";
import type { DecisionDTO } from "../../api/schema";
import { openLocalPath } from "../../api/open";
import { alert, button, card, field, h2 } from "../../components/ui";
import { Reader, scopeName } from "./DocsView";
import { DecisionCard } from "./KnowledgeScreen";
import { joinLocal, MapGraph, type GraphNode } from "./MapGraph";

interface MapViewProps {
  client: DaemonClient;
  repo?: string;
  decisions: DecisionDTO[];
  sources: KbSource[];
  openPath?: (path: string) => Promise<void>;
}

const message = (e: unknown) => e instanceof Error ? e.message : String(e);

export function MapView(props: MapViewProps) {
  return <MapArea key={props.repo} {...props} />;
}

function MapArea({ client, repo, decisions, sources, openPath = openLocalPath }: MapViewProps) {
  const [scopes, setScopes] = useState<DocScope[]>([]);
  const [scopeKey, setScopeKey] = useState("");
  const [input, setInput] = useState("");
  const [area, setArea] = useState<{ paths: string[]; scope: DocScope } | null>(null);
  const [map, setMap] = useState<KnowledgeMap | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<GraphNode | null>(null);
  const [doc, setDoc] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    setScopes([]); setScopeKey(""); setArea(null); setMap(null); setOpen(null); setDoc(null); setError(null);
    if (repo) client.docScopes(repo).then((got) => {
      if (active) { setScopes(got.scopes); setScopeKey(got.scopes[0] ? `${got.scopes[0].kind}:${got.scopes[0].id}` : ""); }
    }, (e: unknown) => { if (active) setError(message(e)); });
    return () => { active = false; };
  }, [client, repo]);
  useEffect(() => {
    let active = true;
    setMap(null); setOpen(null); setDoc(null); setError(null);
    if (repo && area) client.knowledgeMap(repo, area.scope, area.paths).then(
      (got) => { if (active) setMap(got); },
      (e: unknown) => { if (active) setError(message(e)); },
    );
    return () => { active = false; };
  }, [client, repo, area]);
  const graph = useMemo(() => map && repo ? joinLocal(map, decisions, sources, repo) : null, [map, decisions, sources, repo]);
  const titles = useMemo(() => new Map((map?.nodes ?? []).filter((n) => n.kind === "doc").map((n) => [n.doc_id ?? n.id, n.title ?? n.id])), [map]);
  const learn = useCallback(() => {}, []);
  const close = () => setDoc(null);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const scope = scopes.find((s) => `${s.kind}:${s.id}` === scopeKey);
    const paths = [...new Set(input.split(/[,\n]/).map((p) => p.trim()).filter(Boolean))];
    if (scope && paths.length) setArea((current) => {
      const pending = !map && !error;
      const same = current?.scope.kind === scope.kind && current.scope.id === scope.id && JSON.stringify(current.paths) === JSON.stringify(paths);
      return pending && same ? current : { scope, paths };
    });
  };
  const select = (node: GraphNode) => {
    setOpen(node);
    if (node.kind === "doc") setDoc(node.doc_id ?? node.id);
    if (node.kind === "imported") void openPath(node.path).catch((e: unknown) => setError(message(e)));
    if (node.kind === "path" && node.path && area) { setInput(node.path); setArea({ scope: area.scope, paths: [node.path] }); }
  };
  return <section className="flex flex-col gap-s2" aria-label="Map" data-testid="map">
    <header><h2 className={h2}>Knowledge map</h2><p className="text-sm text-ink-2">Explore the docs and decisions around a code area. Decisions and imported folders are connected on this device by shared paths.</p></header>
    {!repo ? <p className="text-ink-3">Choose a repo to explore its knowledge.</p> : <form onSubmit={submit} className={`${card} flex flex-wrap items-end gap-s1 p-s2`}>
      <label className="flex flex-col gap-1 text-xs text-ink-2">Library scope<select className={field} value={scopeKey} onChange={(e) => setScopeKey(e.target.value)}>{scopes.map((s) => <option key={`${s.kind}:${s.id}`} value={`${s.kind}:${s.id}`}>{scopeName(s)}</option>)}</select></label>
      <label className="flex min-w-48 flex-1 flex-col gap-1 text-xs text-ink-2">Code paths<input className={field} value={input} onChange={(e) => setInput(e.target.value)} placeholder="src/cache, src/db" /></label>
      <button type="submit" className={button} disabled={!scopeKey || !input.trim()}>Explore area</button>
    </form>}
    {error && <p role="alert" className={alert}>{error}</p>}
    {repo && !area && <p className="text-sm text-ink-3">Choose code paths to load their neighbourhood.</p>}
    {area && !map && !error && <p role="status">Loading neighbourhood…</p>}
    {graph && <><div className="flex flex-wrap gap-s2 text-xs text-ink-3"><span>{graph.nodes.length} nodes · {graph.edges.length} connections</span><span>Code areas · Documents · Local decisions and folders</span><span>Dashed: supersedes</span></div>
      {graph.nodes.length ? <MapGraph {...graph} onOpen={select} /> : <p>No knowledge is linked to these paths yet.</p>}
    </>}
    {open?.kind === "decision" && <ul><DecisionCard d={open.decision} /></ul>}
    {doc && <Reader key={doc} client={client} repo={repo} id={doc} targets={scopes} titles={titles} onBack={close} onDeleted={close} onOpen={setDoc} onScopes={learn} />}
  </section>;
}
