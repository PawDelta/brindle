import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { alert, button, card, field, primary, quiet } from "../../components/ui";
import { DaemonError, type DaemonClient, type PermissionAction, type PermissionEditorRequest, type PermissionEditorReview, type PermissionEditorRule, type PermissionEditorState } from "../../api/client";

type Choice = { id: string; label: string; detail?: string; tool?: string | null };

/* Native disclosure semantics, with an ordinary searchable button list so
   keyboard and screen reader users can use the same inventory. */
function SearchPicker({ label, choices, selected, onSelect, children }: {
  label: string; choices: Choice[]; selected: string[]; onSelect(id: string): void; children?: ReactNode;
}) {
  const host = useRef<HTMLDetailsElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const id = useId();
  useEffect(() => {
    const outside = (event: PointerEvent) => {
      if (!host.current?.contains(event.target as Node) && host.current) host.current.open = false;
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, []);
  const visible = choices.filter((item) => `${item.label} ${item.detail ?? ""} ${item.id}`.toLowerCase().includes(query.toLowerCase()));
  return <div className="min-w-0">
    <span id={id} className="text-xs font-medium text-ink-3">{label}</span>
    <details ref={host} className="relative mt-1" onToggle={() => { if (host.current?.open) search.current?.focus(); }} onKeyDown={(event) => {
      if (event.key === "Escape" && host.current?.open) {
        event.preventDefault();
        event.stopPropagation();
        host.current.open = false;
        host.current.querySelector("summary")?.focus();
      }
    }}>
      <summary aria-labelledby={id} className={`${field} cursor-pointer`}>{selected.map((value) => choices.find((item) => item.id === value)?.label ?? value).join(", ") || `Choose ${label.toLowerCase()}`}</summary>
      <div className={`${card} absolute left-0 top-full z-50 mt-1 w-full min-w-56 p-s1 shadow-lg`}>
        <input ref={search} type="search" className={`${field} w-full`} aria-label={`Search ${label.toLowerCase()}`} value={query} onChange={(event) => setQuery(event.target.value)} />
        <div className="mt-s1 max-h-56 overflow-auto" role="group" aria-label={`Available ${label.toLowerCase()}`}>
          {visible.map((item) => <button key={item.id} type="button" aria-pressed={selected.includes(item.id)} className={`${quiet} flex w-full flex-col items-start text-left aria-pressed:bg-branch-soft aria-pressed:text-branch`} onClick={() => {
            onSelect(item.id);
            if (host.current) { host.current.open = false; host.current.querySelector("summary")?.focus(); }
          }}><span>{item.label}</span>{item.detail && <> <small className="break-all text-xs text-ink-3">{item.detail}</small></>}</button>)}
          {!visible.length && <p className="p-s1 text-xs text-ink-3">No matches.</p>}
        </div>
        {children}
      </div>
    </details>
  </div>;
}

function RuleDialog({ title, onClose, children }: { title: string; onClose(): void; children: ReactNode }) {
  const host = useRef<HTMLDialogElement>(null);
  const id = useId();
  useEffect(() => {
    const dialog = host.current!;
    dialog.showModal();
    return () => dialog.close();
  }, []);
  return <dialog ref={host} aria-labelledby={id} onCancel={(event) => { event.preventDefault(); onClose(); }} onClose={(event) => {
    // Effect cleanup can queue a close event before StrictMode reopens it.
    if (!event.currentTarget.open) onClose();
  }}
    onClick={(event) => {
      if (event.target !== event.currentTarget) return;
      const bounds = event.currentTarget.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) onClose();
    }}
    className="m-0 ml-auto h-dvh max-h-dvh w-full max-w-2xl border-0 border-l border-line bg-canvas p-0 text-ink backdrop:bg-black/50">
    <header className="sticky top-0 z-40 flex items-center justify-between gap-s2 border-b border-line bg-canvas p-s3">
      <h2 id={id} className="text-base font-semibold">{title}</h2><button type="button" className={button} onClick={onClose} aria-label="Close rule editor">✕</button>
    </header>
    {children}
  </dialog>;
}

/* The editor's draft is deliberately separate from the wire representation:
   the adapter supplies only choices the daemon can enforce. */
type EditorDraft = {
  action: string;
  repo: string;
  target: string;
  matchType: string;
  decision: string;
  lifetime: string;
  duration: number;
};

type EditorInventory = {
  actions: Choice[];
  repos: Choice[];
  lifetimes: Choice[];
  decisions: Choice[];
  matchTypes: Choice[];
  limitations: string[];
};

function RuleEditor({ initial, inventory, busy, error, onSave, onCancel, verifyAction }: {
  initial: EditorDraft; inventory: EditorInventory; busy: boolean; error: string | null;
  onSave(draft: EditorDraft): void; onCancel(): void;
  verifyAction?: (name: string, identifier: string) => Promise<Choice>;
}) {
  const [draft, setDraft] = useState(initial);
  const [custom, setCustom] = useState(false);
  const [name, setName] = useState("");
  const [identifier, setIdentifier] = useState("");
  const [verified, setVerified] = useState<Choice | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [verificationError, setVerificationError] = useState<string | null>(null);
  const verification = useRef(0);
  const [customActions, setCustomActions] = useState<Choice[]>([]);
  const update = <K extends keyof EditorDraft>(key: K, value: EditorDraft[K]) => setDraft((previous) => ({ ...previous, [key]: value }));
  const invalidate = () => { verification.current++; setVerified(null); setVerificationError(null); setVerifying(false); };
  const verify = async () => {
    if (!verifyAction || !name.trim() || !identifier.trim()) return;
    const current = ++verification.current;
    setVerifying(true);
    setVerificationError(null);
    setVerified(null);
    try {
      const result = await verifyAction(name.trim(), identifier.trim());
      if (current === verification.current) setVerified(result);
    } catch (failure) {
      if (current === verification.current) setVerificationError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      if (current === verification.current) setVerifying(false);
    }
  };
  const actions = [...inventory.actions, ...customActions.filter((item) => !inventory.actions.some((existing) => existing.id === item.id))];
  return <form aria-label="Permission rule" onSubmit={(event) => { event.preventDefault(); if (!busy) onSave(draft); }}>
    <fieldset disabled={busy} className="min-w-0">
      <section className="space-y-s2 border-b border-line p-s3">
        <h3 className="text-sm font-semibold">1. Action and people</h3>
        <p className="text-xs text-ink-3">Choose the action and repository this rule covers.</p>
        <SearchPicker label="Actions" choices={actions} selected={[draft.action]} onSelect={(id) => {
          const action = actions.find((item) => item.id === id);
          setDraft((previous) => ({ ...previous, action: id, ...(action?.tool ? { target: action.tool, matchType: "exact" } : {}) }));
        }}>
          {verifyAction && <div className="mt-s1 border-t border-line pt-s1">
            <button type="button" className={quiet} onClick={() => { setCustom(!custom); invalidate(); }}>Add custom action</button>
            {custom && <div className="mt-s1 flex flex-col gap-s1">
              <label className="text-xs text-ink-3">Action name<input className={`${field} mt-1 w-full`} value={name} onChange={(event) => { setName(event.target.value); invalidate(); }} /></label>
              <label className="text-xs text-ink-3">Tool identifier<input className={`${field} mt-1 w-full font-mono`} value={identifier} onChange={(event) => { setIdentifier(event.target.value); invalidate(); }} /></label>
              <button type="button" className={button} disabled={verifying || !name.trim() || !identifier.trim()} onClick={() => void verify()}>Verify tool</button>
              {verificationError && <p role="alert" className={alert}>{verificationError}</p>}
              {verified && <p role="status" className="break-all text-xs text-pass">{verified.label} · {verified.detail}</p>}
              <button type="button" className={button} disabled={!verified} onClick={(event) => {
                if (!verified) return;
                setCustomActions((previous) => [...previous.filter((item) => item.id !== verified.id), verified]);
                setDraft((previous) => ({ ...previous, action: verified.id, target: verified.tool ?? previous.target, matchType: verified.tool ? "exact" : previous.matchType }));
                setCustom(false);
                const picker = event.currentTarget.closest("details");
                if (picker) picker.open = false;
              }}>Use verified action</button>
            </div>}
          </div>}
        </SearchPicker>
        <label className="flex flex-col gap-1 text-xs text-ink-3">Repository<select className={field} value={draft.repo} onChange={(event) => update("repo", event.target.value)}>{inventory.repos.map((repo) => <option key={repo.id} value={repo.id}>{repo.label}</option>)}</select></label>
        <div><button type="button" disabled className={button} aria-label="Roles">Everyone</button><p className="mt-1 text-xs text-ink-3">Role assignment is unavailable because the engine has no authoritative member-role inventory.</p></div>
        <details open className="border-t border-line pt-s1 text-sm text-ink-2">
          <summary className="cursor-pointer">Match a command, file or URL</summary>
          <div className="mt-s2 grid gap-s2 sm:grid-cols-2">
            <label className="flex flex-col gap-1 text-xs text-ink-3">Match type<select disabled={!!actions.find((action) => action.id === draft.action)?.tool} className={field} value={draft.matchType} onChange={(event) => update("matchType", event.target.value)}>{inventory.matchTypes.map((match) => <option key={match.id} value={match.id}>{match.label}</option>)}</select></label>
            <label className="flex flex-col gap-1 text-xs text-ink-3">Target<input readOnly={!!actions.find((action) => action.id === draft.action)?.tool} required className={`${field} font-mono`} value={draft.target} onChange={(event) => update("target", event.target.value)} /></label>
          </div>
        </details>
      </section>
      <section className="space-y-s2 border-b border-line p-s3">
        <h3 className="text-sm font-semibold">2. Access</h3>
        <label className="flex flex-col gap-1 text-xs text-ink-3">Decision<select className={field} value={draft.decision} onChange={(event) => update("decision", event.target.value)}>{inventory.decisions.map((decision) => <option key={decision.id} value={decision.id}>{decision.label}</option>)}</select></label>
        {inventory.limitations.map((limitation) => <p key={limitation} className="text-xs text-ink-3">{limitation}</p>)}
      </section>
      <section className="space-y-s2 border-b border-line p-s3">
        <h3 className="text-sm font-semibold">3. Lifetime</h3>
        <label className="flex flex-col gap-1 text-xs text-ink-3">Lifetime<select className={field} value={draft.lifetime} onChange={(event) => update("lifetime", event.target.value)}>{inventory.lifetimes.map((lifetime) => <option key={lifetime.id} value={lifetime.id}>{lifetime.label}</option>)}</select></label>
        {draft.lifetime === "time" && <label className="flex flex-col gap-1 text-xs text-ink-3">Duration in minutes<input className={field} type="number" required min={1} value={draft.duration} onChange={(event) => update("duration", Number(event.target.value))} /></label>}
      </section>
    </fieldset>
    <p aria-live="polite" className="break-words bg-branch-soft p-s3 text-xs text-ink-2">Everyone · {actions.find((action) => action.id === draft.action)?.label} · {inventory.decisions.find((decision) => decision.id === draft.decision)?.label}</p>
    {error && <p role="alert" className={`${alert} m-s3`}>{error}</p>}
    <footer className="sticky bottom-0 flex justify-end gap-s1 border-t border-line bg-canvas p-s3"><button type="button" className={quiet} disabled={busy} onClick={onCancel}>Cancel</button><button type="submit" className={primary} disabled={busy || !draft.action || !draft.target.trim()}>Save rule</button></footer>
  </form>;
}

type RuleView = {
  id: string;
  action: string;
  decision: string;
  roles: string[];
  repository: string;
  target: string;
  matchType: string;
  lifetime: string;
  source: string;
  editable: boolean;
  exclusions: string[];
};

function RuleScope({ rule, title }: { rule: RuleView; title: string }) {
  return <article className={`${card} min-w-0 space-y-s1 p-s2`}>
    <h4 className="text-xs font-medium uppercase tracking-wide text-ink-3">{title}</h4>
    <p className="text-sm font-medium">{rule.action} · {rule.decision}</p>
    <dl className="grid grid-cols-[auto_1fr] gap-x-s2 gap-y-1 break-words text-xs">
      <dt className="text-ink-3">Roles</dt><dd>{rule.roles.join(", ")}</dd>
      <dt className="text-ink-3">Repository</dt><dd>{rule.repository}</dd>
      <dt className="text-ink-3">Target</dt><dd className="break-all font-mono">{rule.matchType} · {rule.target}</dd>
      <dt className="text-ink-3">Lifetime</dt><dd>{rule.lifetime}</dd>
      <dt className="text-ink-3">Source</dt><dd>{rule.source}{!rule.editable && " · read only"}</dd>
      {rule.exclusions.length > 0 && <><dt className="text-ink-3">Except</dt><dd>{rule.exclusions.map((scope, index) => <p key={index}>{scope}</p>)}</dd></>}
    </dl>
  </article>;
}

type ResolutionChoice = { id: string; label: string; enabled: boolean; reason?: string };
type ReviewView = {
  draft: RuleView | null;
  conflicts: { existing: RuleView; reason: string }[];
  choices: ResolutionChoice[];
  added: RuleView[];
  updated: { before: RuleView; after: RuleView }[];
  removed: RuleView[];
  blockers: string[];
  canSave: boolean;
  selected?: string;
};

function ConflictReview({ review, busy, error, onChoice, onApply, onBack }: {
  review: ReviewView; busy: boolean; error: string | null;
  onChoice(id: string): void; onApply(): void; onBack(): void;
}) {
  return <section className="space-y-s2 p-s3" aria-label="Review rule changes">
    {review.conflicts.length > 0 && <>
      <div className="space-y-s1 rounded-lg border border-sap p-s2">
        <h3 className="font-semibold">Different decisions on overlapping scope</h3>
        <p className="text-sm text-ink-2">Compare the roles, repositories and targets before choosing a resolution.</p>
      </div>
      <div className="grid gap-s2 sm:grid-cols-2">
        {review.draft && <RuleScope rule={review.draft} title="Your unsaved rule" />}
        {review.conflicts.map(({ existing, reason }) => <div key={existing.id} className="space-y-s1"><RuleScope rule={existing} title={`Existing rule ${existing.id}`} /><p className="text-xs text-ink-3">{reason}</p></div>)}
      </div>
    </>}
    {review.choices.length > 0 && <fieldset disabled={busy} className="flex flex-wrap gap-s1">
      <legend className="mb-s1 text-sm font-medium">Choose what to keep</legend>
      {review.choices.map((choice) => <div key={choice.id}><button type="button" className={button} aria-pressed={review.selected === choice.id} disabled={!choice.enabled} onClick={() => onChoice(choice.id)}>{choice.label}</button>{choice.reason && <p className="mt-1 max-w-56 text-xs text-ink-3">{choice.reason}</p>}</div>)}
    </fieldset>}
    <h3 className="text-sm font-semibold">Proposed changes</h3>
    <p className="text-xs text-ink-3">{review.added.length} added · {review.updated.length} updated · {review.removed.length} removed</p>
    <div className="space-y-s2" aria-label="Proposed rules">
      {review.added.map((rule) => <RuleScope key={`added-${rule.id}`} rule={rule} title="Will be added" />)}
      {review.updated.map(({ before, after }) => <div key={`updated-${after.id}`} className="grid gap-s1 sm:grid-cols-2"><RuleScope rule={before} title="Before update" /><RuleScope rule={after} title="After update" /></div>)}
      {review.removed.map((rule) => <RuleScope key={`removed-${rule.id}`} rule={rule} title="Will be removed" />)}
    </div>
    {review.blockers.length > 0 && <div role="alert" className={alert}>{review.blockers.map((reason, index) => <p key={index}>{reason}</p>)}</div>}
    {error && <p role="alert" className={alert}>{error}</p>}
    <footer className="sticky bottom-0 flex justify-end gap-s1 border-t border-line bg-canvas py-s2">
      <button type="button" className={quiet} disabled={busy} onClick={onBack}>Back to editing</button>
      <button type="button" className={primary} disabled={busy || !review.canSave} onClick={onApply}>Save resolved rules</button>
    </footer>
  </section>;
}

function RuleTable({ rules, busy, onEdit, onRemove }: { rules: RuleView[]; busy: boolean; onEdit(rule: RuleView): void; onRemove(rule: RuleView): void }) {
  const [query, setQuery] = useState("");
  const visible = rules.filter((rule) => [rule.action, rule.decision, rule.target, rule.repository, rule.roles.join(" "), rule.source].join(" ").toLowerCase().includes(query.toLowerCase()));
  return <div className="space-y-s2">
    <input type="search" aria-label="Search rules" className={`${field} w-full`} value={query} onChange={(event) => setQuery(event.target.value)} />
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs text-ink-3"><tr>{["Action and target", "Access", "Roles and scope", ""].map((heading) => <th key={heading} className="border-b border-line py-s1 pr-s2 font-medium">{heading}</th>)}</tr></thead>
        <tbody>{visible.map((rule) => <tr key={rule.id} data-testid="permission-rule">
          <td className="border-b border-line py-s2 pr-s2"><span className="font-medium">{rule.action}</span><code className="mt-1 block break-all text-xs text-ink-3">{rule.matchType} · {rule.target}</code>{rule.exclusions.length > 0 && <small className="block text-ink-3">Except: {rule.exclusions.join("; ")}</small>}</td>
          <td className="border-b border-line py-s2 pr-s2"><span>{rule.decision}</span><small className="mt-1 block text-xs text-ink-3">{rule.lifetime}</small></td>
          <td className="border-b border-line py-s2 pr-s2"><span>{rule.roles.join(", ")}</span><small className="mt-1 block text-xs text-ink-3">{rule.repository} · {rule.source}</small></td>
          <td className="border-b border-line py-s2"><div className="flex items-center gap-1">
            <button type="button" className={`${quiet} p-s1`} aria-label={`Edit rule ${rule.id}`} title="Edit rule" disabled={busy || !rule.editable} onClick={() => onEdit(rule)}><svg aria-hidden width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="m16 3 5 5M3 21l5-1L21 7a2 2 0 0 0-5-5L3 15z" /></svg></button>
            <button type="button" className={`${quiet} p-s1 hover:text-sap`} aria-label={`Remove rule ${rule.id}`} title="Remove rule" disabled={busy || !rule.editable} onClick={() => onRemove(rule)}><svg aria-hidden width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7" /></svg></button>
          </div></td>
        </tr>)}</tbody>
      </table>
      {visible.length === 0 && <p className="py-s2 text-sm text-ink-3">No matching rules.</p>}
    </div>
  </div>;
}

const actionChoice = (action: PermissionAction): Choice => ({ id: action.id, label: action.name, detail: action.tool ?? action.source, tool: action.tool });
const failureMessage = (failure: unknown) => failure instanceof Error ? failure.message : String(failure);

export function PermissionRules({ client, repo }: { client: DaemonClient; repo: string | null }) {
  const [state, setState] = useState<PermissionEditorState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const [editor, setEditor] = useState<{ draft: EditorDraft; original: PermissionEditorRule | null; originalDuration: number } | null>(null);
  const [request, setRequest] = useState<PermissionEditorRequest | null>(null);
  const [review, setReview] = useState<PermissionEditorReview | null>(null);
  const [manual, setManual] = useState<PermissionEditorReview | null>(null);
  const [invalidated, setInvalidated] = useState(false);
  const [showPresets, setShowPresets] = useState(false);
  useEffect(() => {
    let active = true;
    setState(null);
    setError(null);
    if (repo) void client.permissionEditor(repo).then((value) => { if (active) setState(value); }).catch((failure) => { if (active) setError(failureMessage(failure)); });
    return () => { active = false; };
  }, [client, repo]);

  const run = async (work: () => Promise<void>) => {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setError(null);
    try { await work(); } catch (failure) { setError(failureMessage(failure)); }
    finally { pending.current = false; setBusy(false); }
  };
  const close = () => { if (!pending.current) { setEditor(null); setReview(null); setManual(null); setRequest(null); setError(null); } };
  const view = (rule: PermissionEditorRule): RuleView => ({
    id: rule.id,
    action: state?.catalog.find((action) => action.kind === rule.kind && action.tool === rule.match)?.name
      ?? state?.catalog.find((action) => action.kind === rule.kind && !action.tool)?.name ?? rule.kind,
    decision: rule.decision,
    roles: ["Everyone"],
    repository: rule.repo_root ?? "All repositories",
    target: rule.match,
    matchType: rule.match_type,
    lifetime: rule.expires_at == null ? "Until revoked" : `Until ${new Date(rule.expires_at * 1000).toLocaleString()}`,
    source: rule.source,
    editable: rule.editable === true && !rule.protected,
    exclusions: (rule.exclusions ?? []).map((scope) => `${scope.kind} ${scope.match_type} ${scope.match} · ${scope.repo_root ?? "All repositories"} · ${scope.expires_at == null ? "Until revoked" : new Date(scope.expires_at * 1000).toLocaleString()}`),
  });
  const actionFor = (rule: PermissionEditorRule): PermissionAction => state?.catalog.find((action) => action.kind === rule.kind
    && (action.tool ? rule.match_type === "exact" && action.tool === rule.match : true))
    ?? { id: `existing:${rule.id}`, kind: rule.kind, name: `Existing ${rule.kind} scope`, tool: null, source: "Existing rule scope; not a verified action" };
  const originalAction = editor?.original ? actionFor(editor.original) : null;
  const draftFor = (rule?: PermissionEditorRule): EditorDraft => ({
    action: rule ? actionFor(rule).id : (state?.catalog.find((action) => action.kind === "bash" && !action.tool) ?? state?.catalog[0])?.id ?? "",
    repo: rule?.repo_root ?? "", target: rule?.match ?? "", matchType: rule?.match_type ?? "exact",
    decision: rule?.decision ?? "ask", lifetime: rule?.expires_at == null ? "forever" : "time",
    duration: rule?.expires_at == null ? 30 : Math.max(1, Math.ceil((rule.expires_at * 1000 - Date.now()) / 60000)),
  });
  const begin = (rule?: PermissionEditorRule) => {
    const draft = draftFor(rule);
    setError(null);
    setEditor({ draft, original: rule?.id ? rule : null, originalDuration: draft.duration });
  };
  const inspect = (body: PermissionEditorRequest) => run(async () => {
    try {
      const result = await client.reviewPermissionRule(body);
      setRequest(body);
      setReview(result);
      if (body.resolution === "manual") setManual(result);
      setInvalidated(false);
    } catch (failure) {
      if (failure instanceof DaemonError && failure.status === 409) {
        setReview(null); setManual(null); setRequest(null); setInvalidated(true);
        setState(await client.permissionEditor(body.repo));
      }
      throw failure;
    }
  });
  const submit = (draft: EditorDraft) => {
    if (!state || !repo || !editor) return;
    const action = state.catalog.find((item) => item.id === draft.action) ?? (originalAction?.id === draft.action ? originalAction : null);
    if (!action) { setError("Choose an available action."); return; }
    const original = editor.original;
    const expires = draft.lifetime === "forever" ? null
      : original?.expires_at != null && draft.duration === editor.originalDuration
        ? original.expires_at : Date.now() / 1000 + draft.duration * 60;
    setEditor({ ...editor, draft });
    void inspect({ repo, revision: state.revision, operation: "upsert", resolution: "manual", ...(original ? { id: original.id } : {}),
      rule: { kind: action.kind, match: action.tool ?? draft.target, match_type: (action.tool ? "exact" : draft.matchType) as PermissionEditorRule["match_type"],
        decision: draft.decision as PermissionEditorRule["decision"], repo_root: draft.repo || null, expires_at: expires,
        exclusions: original?.kind === action.kind ? original.exclusions ?? [] : [] } });
  };
  const apply = () => {
    if (!review || !request || !review.can_save || invalidated) return;
    void run(async () => {
      try {
        const saved = await client.savePermissionRule({ ...request, proposal: review.proposal });
        setState((previous) => previous ? { ...previous, ...saved } : previous);
        setEditor(null); setReview(null); setManual(null); setRequest(null);
      } catch (failure) { setInvalidated(true); throw failure; }
    });
  };
  const back = () => void run(async () => {
    if (repo) setState(await client.permissionEditor(repo));
    setReview(null); setManual(null); setRequest(null);
  });

  if (!repo) return <p className="text-sm text-ink-3">Choose a repository to review permission rules.</p>;
  if (!state) return <div>{error ? <><p role="alert" className={alert}>{error}</p><button type="button" className={button} disabled={busy} onClick={() => void run(async () => setState(await client.permissionEditor(repo)))}>Load rules</button></> : <p role="status" className="text-sm text-ink-3">Loading rules…</p>}</div>;
  const inventory: EditorInventory = {
    actions: [...state.catalog.map(actionChoice), ...(originalAction && !state.catalog.some((action) => action.id === originalAction.id) ? [actionChoice(originalAction)] : [])],
    limitations: state.limitations,
    repos: [{ id: "", label: "All repositories" }, ...(state.restrictions.repo_supported ? [{ id: repo, label: repo }] : [])],
    lifetimes: [{ id: "forever", label: "Until revoked" }, ...(state.restrictions.expiry_supported ? [{ id: "time", label: "Custom duration" }] : [])],
    decisions: [{ id: "ask", label: "Ask for approval" }, { id: "allow", label: "Allow automatically" }, { id: "deny", label: "Deny" }],
    matchTypes: [{ id: "exact", label: "Is exactly" }, { id: "prefix", label: "Starts with" }, { id: "glob", label: "Matches pattern" }],
  };
  const baseline = manual ?? review;
  const rulesById = new Map([...state.rules, ...(baseline?.rules ?? [])].map((rule) => [rule.id, rule]));
  const conflicts = (baseline?.conflicts ?? []).flatMap((conflict) => [conflict.left, conflict.right].flatMap((id) => {
    const existing = state.rules.find((rule) => rule.id === id);
    return existing ? [{ existing: view(existing), reason: conflict.overlap === "unknown" ? "Overlap cannot be established safely." : "These scopes have different decisions." }] : [];
  }));
  const draft = baseline?.changes.find((change) => change.after && change.type !== "removed")?.after;
  const reviewView: ReviewView | null = review ? {
    draft: draft ? view(draft) : null,
    conflicts: conflicts.filter((conflict, index) => conflicts.findIndex((item) => item.existing.id === conflict.existing.id) === index),
    choices: (baseline?.conflicts.length ?? 0) > 0 ? [
      { id: "existing", label: "Keep existing", enabled: !busy },
      { id: "new", label: "Use new rule", enabled: !busy && !!request?.rule && !(baseline?.conflicts.some((conflict) => conflict.protected || conflict.overlap === "unknown")), reason: baseline?.conflicts.some((conflict) => conflict.protected) ? "Fixed protections cannot be changed." : undefined },
      { id: "auto", label: "Auto resolve", enabled: !busy && !baseline?.conflicts.some((conflict) => conflict.overlap === "unknown"), reason: "Keep stricter decisions and narrow overlapping editable rules." },
    ] : [],
    selected: request?.resolution,
    added: review.changes.flatMap((change) => change.type === "added" && change.after ? [view(change.after)] : []),
    updated: review.changes.flatMap((change) => change.type === "updated" && change.before && change.after ? [{ before: view(change.before), after: view(change.after) }] : []),
    removed: review.changes.flatMap((change) => change.type === "removed" && change.before ? [view(change.before)] : []),
    blockers: [...review.unresolved.map((conflict) => `${conflict.overlap === "unknown" ? "Unknown overlap" : "Unresolved conflict"}: ${rulesById.get(conflict.left)?.match ?? conflict.left} / ${rulesById.get(conflict.right)?.match ?? conflict.right}`), ...(invalidated ? ["This proposal must be reviewed again before saving."] : [])],
    canSave: review.can_save && !invalidated,
  } : null;
  const enforcementNotice = state.restrictions.permission_policy === "off"
    ? <p role="status" aria-label="Permission enforcement" className={alert}>Permission enforcement is off for this repository. Rules can be saved, but are not enforced until permission policy is enabled.</p>
    : null;
  return <div className="w-full space-y-s2">
    {enforcementNotice}
    <div className="flex flex-wrap items-center gap-s1"><button type="button" className={primary} disabled={busy || !state.restrictions.editable} onClick={() => begin()}>Add rule</button><button type="button" className={button} disabled={busy || !state.restrictions.editable} onClick={() => setShowPresets(!showPresets)}>Presets</button></div>
    {state.restrictions.reason && <p className="text-xs text-ink-3">{state.restrictions.reason}</p>}
    {state.limitations.map((limitation) => <p key={limitation} className="text-xs text-ink-3">{limitation}</p>)}
    {showPresets && <div className="grid gap-s2 sm:grid-cols-3" aria-label="Permission presets">{state.presets.map((preset) => <article key={preset.id} className={`${card} p-s2`}><h3 className="text-sm font-medium">{preset.name}</h3><p className="my-s1 break-all text-xs text-ink-3">{preset.rule.kind} · {preset.rule.decision} · {preset.rule.match}</p><button type="button" className={button} disabled={busy || !state.restrictions.editable} onClick={() => begin({ ...preset.rule, id: "" })}>Use {preset.name}</button></article>)}</div>}
    <RuleTable rules={state.rules.map(view)} busy={busy || !state.restrictions.editable} onEdit={(rule) => begin(state.rules.find((item) => item.id === rule.id))} onRemove={(rule) => void inspect({ repo, revision: state.revision, operation: "remove", id: rule.id, resolution: "manual" })} />
    {error && !editor && !review && <p role="alert" className={alert}>{error}</p>}
    {(editor || reviewView) && <RuleDialog title={reviewView ? "Review rule changes" : editor?.original?.id ? "Edit rule" : "Add rule"} onClose={close}>
      {enforcementNotice}
      {reviewView ? <ConflictReview review={reviewView} busy={busy} error={error} onChoice={(resolution) => { if (request) void inspect({ ...request, resolution: resolution as PermissionEditorRequest["resolution"] }); }} onApply={apply} onBack={back} />
        : editor && <RuleEditor key={editor.original?.id ?? "new"} initial={editor.draft} inventory={inventory} busy={busy || !state.restrictions.editable} error={error} onSave={submit} onCancel={close} verifyAction={async (name, identifier) => {
          const result = await client.verifyPermissionAction(identifier);
          if (!result.verified || !result.action) throw new Error("This tool is not in the engine's current inventory.");
          const action = { ...result.action, name };
          setState((previous) => previous ? { ...previous, catalog: [...previous.catalog.filter((item) => item.id !== action.id), action] } : previous);
          return actionChoice(action);
        }} />}
    </RuleDialog>}
  </div>;
}
