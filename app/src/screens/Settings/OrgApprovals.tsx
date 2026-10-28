/* Settings › Organization: the organization's approval administration
   (daemon/routes_org_permissions.py, over the account API). Its members and
   roles (GET /v1/org-permissions; Assign role: PUT
   /v1/org-permissions/role-assignment) and the approval limits for the
   organization default or one role (PUT /v1/org-permissions/policy, pinned
   to the policy version it was read at). Nothing is read until the person
   opens it; once open it stays mounted while hidden, so a draft survives
   closing and opening it again. */

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { DaemonError, type DaemonClient } from "../../api/client";
import type { OrgGrantPolicyDTO, OrgPermissionsDTO } from "../../api/schema";
import { alert, button, card, field, primary, quiet } from "../../components/ui";

type Choice = { id: string; label: string; detail?: string };

const failureMessage = (failure: unknown) => (failure instanceof Error ? failure.message : String(failure));

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

/* Opens on demand (a person outside any organization never reads it); once opened, hiding keeps its drafts. */
export function OrgApprovals({ client }: { client: DaemonClient }) {
  const [open, setOpen] = useState(false);
  const [opened, setOpened] = useState(false);
  return <div className="w-full space-y-s2">
    <button type="button" className={button} aria-expanded={open} onClick={() => { setOpened(true); setOpen((o) => !o); }}>
      {open ? "Hide organization approvals" : "Organization approvals"}
    </button>
    {opened && <div hidden={!open}><OrganizationPermissions client={client} /></div>}
  </div>;
}

type GrantDraft = Required<OrgGrantPolicyDTO>;
const grantDraft = (grant?: OrgGrantPolicyDTO | null): GrantDraft => ({
  allow: false, kinds: [], overridable: [], max_minutes: 60, max_uses: 20,
  deny: [], allow_persistent: false, lifetimes: ["session", "time"], scopes: ["branch"], controls: [], org_name: "", ...grant,
});

/* brindle.permission_grants.ORG_GROUPINGS: what an organization may Block or Ask for as a whole. */
const GROUPINGS: [string, string, string][] = [
  ["git/readonly", "Read-only git commands", "status, diff, log, show"],
  ["shell/readonly", "Read-only shell commands", "grep, ls, cat, head"],
  ["dev/tests", "Test runs", "pytest, npm test"],
  ["dev/queries", "Package and GitHub queries", "npm ls, gh pr view"],
  ["git/local", "Local git changes", "add, commit, branch, stash"],
  ["dev/install", "Package installs into the project", "npm install, uv add in a checkout"],
  ["files/write-worktree", "File changes in the checkout", "writes, edits, moves, deletes"],
  ["shell/unchecked", "Unchecked commands", "commands brindle can't verify"],
  ["network/fetch", "Network fetches", "curl, wget, web fetches"],
  ["deploy/trigger", "Deploys, releases and workflow runs", "gh workflow run, gh release, terraform apply, cloud deploys"],
  ["packages/install", "Package installs", "any installer, anywhere"],
];
const KINDS = ["bash", "read", "write", "edit", "fetch", "mcp", "other"] as const;
const MATCHES: [string, string][] = [["prefix", "Starts with"], ["exact", "Is exactly"], ["glob", "Matches pattern"]];
type Control = GrantDraft["controls"][number];
const splitRule = (rule: string): [string, string, string] => {
  const [kind = "bash", type = "prefix", ...rest] = rule.split(" ");
  return [kind, type, rest.join(" ")];
};

/* Block or Ask, set by the organization above every member's preset, rules and saved approvals. */
function OrgRules({ controls, onChange }: { controls: Control[]; onChange(controls: Control[]): void }) {
  const grouped = new Map(controls.filter((c) => c.group).map((c) => [c.group!, c]));
  const patterns = controls.filter((c) => c.rule != null);
  const setGroup = (id: string, next: Control | null) => onChange([...controls.filter((c) => c.group !== id), ...(next ? [next] : [])]);
  const setPattern = (i: number, next: Control | null) => {
    const rest = patterns.filter((_, j) => j !== i);
    if (next) rest.splice(i, 0, next);
    onChange([...controls.filter((c) => c.group), ...rest]);
  };
  return <fieldset className="space-y-s2">
    <legend className="text-sm font-semibold">Organization rules</legend>
    <p className="text-xs text-ink-3">Block or Ask over a whole kind of action, or one pattern. These win over every member’s preset, rules and saved approvals; members see the reason.</p>
    <ul className="divide-y divide-line rounded-md border border-line" aria-label="Groupings">
      {GROUPINGS.map(([id, label, hint]) => {
        const c = grouped.get(id);
        return <li key={id} className="grid gap-s1 px-s2 py-s1 sm:grid-cols-[minmax(0,1fr)_auto]">
          <span className="text-sm">{label} <small className="block text-xs text-ink-3">{hint}</small></span>
          <select className={field} aria-label={`${label} setting`} value={c?.decision ?? ""} onChange={(event) => setGroup(id, event.target.value ? { decision: event.target.value as Control["decision"], group: id, reason: c?.reason ?? "" } : null)}>
            <option value="">Members decide</option><option value="ask">Ask</option><option value="deny">Block</option>
          </select>
          {c && <input className={`${field} sm:col-span-2`} aria-label={`${label} reason`} placeholder="Reason members see" maxLength={300} value={c.reason ?? ""} onChange={(event) => setGroup(id, { ...c, reason: event.target.value })} />}
        </li>;
      })}
    </ul>
    <div className="space-y-s1" aria-label="Patterns">
      {patterns.map((c, i) => {
        const [kind, type, match] = splitRule(c.rule!);
        const rule = (k: string, t: string, m: string) => setPattern(i, { ...c, rule: `${k} ${t} ${m}` });
        return <div key={i} className="grid gap-s1 rounded-md border border-line p-s1 sm:grid-cols-[auto_auto_auto_minmax(0,1fr)_auto]">
          <select className={field} aria-label="Pattern setting" value={c.decision} onChange={(event) => setPattern(i, { ...c, decision: event.target.value as Control["decision"] })}><option value="ask">Ask</option><option value="deny">Block</option></select>
          <select className={field} aria-label="Pattern kind" value={kind} onChange={(event) => rule(event.target.value, type, match)}>{KINDS.map((k) => <option key={k} value={k}>{k}</option>)}</select>
          <select className={field} aria-label="Pattern match" value={type} onChange={(event) => rule(kind, event.target.value, match)}>{MATCHES.map(([t, l]) => <option key={t} value={t}>{l}</option>)}</select>
          <input className={`${field} font-mono`} aria-label="Pattern" required value={match} onChange={(event) => rule(kind, type, event.target.value)} />
          <button type="button" className={quiet} onClick={() => setPattern(i, null)}>Remove</button>
          <input className={`${field} sm:col-span-5`} aria-label="Pattern reason" placeholder="Reason members see" maxLength={300} value={c.reason ?? ""} onChange={(event) => setPattern(i, { ...c, reason: event.target.value })} />
        </div>;
      })}
      <button type="button" className={button} onClick={() => onChange([...controls, { decision: "deny", rule: "bash prefix ", reason: "" }])}>Add a pattern</button>
    </div>
  </fieldset>;
}

function GrantChoices<T extends string>({ label, choices, selected, onChange }: {
  label: string; choices: readonly T[]; selected: T[]; onChange(values: T[]): void;
}) {
  return <fieldset className="space-y-s1"><legend className="text-xs font-medium text-ink-3">{label}</legend>
    <div className="flex flex-wrap gap-s2">{choices.map((value) => <label key={value} className="flex items-center gap-1 text-sm">
      <input type="checkbox" checked={selected.includes(value)} onChange={(event) => onChange(event.target.checked ? [...selected, value] : selected.filter((item) => item !== value))} />{value}
    </label>)}</div>
  </fieldset>;
}

function OrganizationPermissions({ client }: { client: DaemonClient }) {
  const [state, setState] = useState<OrgPermissionsDTO | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const [member, setMember] = useState("");
  const [assignment, setAssignment] = useState("");
  const [role, setRole] = useState("");
  const [drafts, setDrafts] = useState(() => new Map<string, GrantDraft>());
  const [stale, setStale] = useState(false);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    let active = true;
    void client.orgPermissions().then((value) => { if (active) setState(value); })
      .catch((failure) => { if (active) setError(failureMessage(failure)); });
    return () => { active = false; };
  }, [client]);
  const run = async (work: () => Promise<void>, keepError = false) => {
    if (pending.current) return;
    pending.current = true; setBusy(true); setSaved(false);
    if (!keepError) setError(null);
    try { await work(); } catch (failure) {
      setError(failureMessage(failure));
      // A stale version needs a reload; an account API without the scoped
      // save cannot take this edit at all, so stop offering it (never fall back).
      if (failure instanceof DaemonError && (failure.status === 409 || failure.code === "scoped_save_unsupported")) setStale(true);
    } finally { pending.current = false; setBusy(false); }
  };
  const reload = () => run(async () => {
    const loaded = await client.orgPermissions();
    if (state && JSON.stringify(loaded.identity) !== JSON.stringify(state.identity)) {
      setStale(true);
      throw new Error("Your account identity changed. Reopen Organization settings before editing; your current drafts have been kept here.");
    }
    setState(loaded); setStale(false);
  }, true);
  if (!state) return <section className={`${card} space-y-s2 p-s3`} aria-label="Organization permissions">
    {error ? <><p role="alert" className={alert}>{error}</p><button className={button} disabled={busy} onClick={() => void reload()}>Load organization</button></>
      : <p role="status" className="text-sm text-ink-3">Loading organization…</p>}
  </section>;
  const person = state.members.find((item) => item.sub === member);
  const currentRole = person?.policy_role || person?.role;
  const roleChoices = state.roles.map((item) => ({ id: item.name, label: item.name,
    detail: `${item.builtin ? "Built-in" : "Custom"} · ${item.holders?.length ?? 0} members · ${item.permissions.join(", ") || "No administrative permissions"}` }));
  const own = role ? (Object.hasOwn(state.policy.roles, role) ? state.policy.roles[role] : null) : state.policy.permission_grants;
  const draft = drafts.get(role) ?? grantDraft(own ?? state.policy.permission_grants);
  const update = <K extends keyof GrantDraft>(key: K, value: GrantDraft[K]) => {
    setDrafts((previous) => new Map(previous).set(role, { ...draft, [key]: value })); setSaved(false);
  };
  const save = (inherit = false) => run(async () => {
    // Capture even an untouched draft so a conflict never resets the form.
    setDrafts((previous) => new Map(previous).set(role, draft));
    const policy = await client.saveOrgApprovalPolicy({ org_id: state.identity.org_id, actor_sub: state.identity.sub,
      if_version: state.policy.version, role: role || null, permission_grants: inherit ? null : draft });
    setState({ ...state, policy });
    setDrafts((previous) => { const next = new Map(previous); next.delete(role); return next; });
    setSaved(true);
  });
  const locked = busy || stale || !state.can_edit_policy || (!!role && !state.roles.some((item) => item.name === role));
  return <section className="space-y-s3" aria-label="Organization permissions">
    <div className={`${card} space-y-s2 p-s3`}>
      <h2 className="text-base font-semibold">Organization members and roles</h2>
      <p className="break-all text-xs text-ink-3">{state.identity.org_id} · {state.identity.sub} · {state.identity.policy_role || state.identity.role}</p>
      {state.inventory_error ? <p role="status" className="text-sm text-ink-3">Member and role inventory unavailable: {state.inventory_error}. Base approval limits remain available; the account checks control permission when you save.</p> : <>
      <fieldset disabled={busy} className="grid min-w-0 gap-s2 sm:grid-cols-2">
        <SearchPicker label="Members" choices={state.members.map((item) => ({ id: item.sub, label: item.email || item.sub,
          detail: `${item.sub} · ${item.policy_role || item.role}` }))} selected={member ? [member] : []} onSelect={(sub) => {
          setMember(sub); const target = state.members.find((item) => item.sub === sub);
          setAssignment(target?.policy_role || target?.role || "");
        }} />
        <SearchPicker label="Assignment roles" choices={roleChoices} selected={assignment ? [assignment] : []} onSelect={setAssignment} />
      </fieldset>
      <button type="button" className={primary} disabled={busy || !person || !assignment || assignment === currentRole} onClick={() => void run(async () => {
        await client.assignOrgRole({ org_id: state.identity.org_id, actor_sub: state.identity.sub, sub: member, role: assignment });
        const loaded = await client.orgPermissions();
        // Assigning the signed-in person may change their authority. Keep any
        // draft visible, but require a new editor with the new identity.
        if (JSON.stringify(loaded.identity) !== JSON.stringify(state.identity)) { setStale(true); return; }
        setState((previous) => previous ? { ...previous, roles: loaded.roles, members: loaded.members,
          inventory_error: loaded.inventory_error,
          can_edit_policy: loaded.can_edit_policy, policy_readonly_reason: loaded.policy_readonly_reason } : previous);
      })}>Assign role</button>
      <p className="text-xs text-ink-3">Assignment replaces the member’s current role. The account checks your permissions and protects the last owner.</p>
      </>}
    </div>
    <form className={`${card} space-y-s2 p-s3`} aria-label="Organization approval limits" onSubmit={(event) => { event.preventDefault(); if (!locked) void save(); }}>
      <h2 className="text-base font-semibold">Approval limits</h2>
      <p className="text-xs text-ink-3">Policy version {state.policy.version}. A role override replaces the entire approval-limit block, including denied targets. Independent protected hard denies still apply.</p>
      <fieldset disabled={busy}>
        <SearchPicker label="Approval roles" choices={[{ id: "", label: "Organization default" }, ...roleChoices]} selected={[role]} onSelect={(value) => { setRole(value); setSaved(false); }} />
      </fieldset>
      {role && !own && <p className="text-xs text-ink-3">This role inherits limits. The draft starts from the organization default. Custom roles also inherit their built-in role’s limits when no custom override is set.</p>}
      {state.policy_readonly_reason && <p className="text-sm text-ink-3">{state.policy_readonly_reason}</p>}
      <details className="text-xs text-ink-3"><summary className="cursor-pointer">Current saved limits for comparison</summary>
        <pre className="mt-s1 overflow-auto whitespace-pre-wrap">{own ? JSON.stringify(own, null, 2) : role ? "Inherited" : "No saved approvals permitted"}</pre>
      </details>
      <fieldset disabled={busy || !state.can_edit_policy} className="min-w-0 space-y-s2">
        <label className="flex items-center gap-s1 text-sm"><input type="checkbox" checked={draft.allow} onChange={(event) => update("allow", event.target.checked)} />Allow members to save approvals</label>
        <GrantChoices label="Tool kinds" choices={["read", "write", "edit", "bash", "fetch", "mcp", "other"] as const} selected={draft.kinds} onChange={(values) => update("kinds", values)} />
        <GrantChoices label="Actions members may approve" choices={["git-push", "env"] as const} selected={draft.overridable} onChange={(values) => update("overridable", values)} />
        <GrantChoices label="Allowed scopes" choices={["branch", "remote", "repo", "all"] as const} selected={draft.scopes} onChange={(values) => update("scopes", values)} />
        <GrantChoices label="Allowed lifetimes" choices={["session", "time", "forever"] as const} selected={draft.lifetimes} onChange={(values) => update("lifetimes", values)} />
        <label className="flex items-center gap-s1 text-sm"><input type="checkbox" checked={draft.allow_persistent} onChange={(event) => update("allow_persistent", event.target.checked)} />Allow approvals to persist across restarts</label>
        <div className="grid gap-s2 sm:grid-cols-2">
          <label className="flex flex-col gap-1 text-xs text-ink-3">Maximum minutes<input type="number" required min={1} max={1440} className={field} value={draft.max_minutes} onChange={(event) => update("max_minutes", Number(event.target.value))} /></label>
          <label className="flex flex-col gap-1 text-xs text-ink-3">Maximum uses<input type="number" required min={1} max={1000} className={field} value={draft.max_uses} onChange={(event) => update("max_uses", Number(event.target.value))} /></label>
        </div>
        <label className="flex flex-col gap-1 text-xs text-ink-3">Denied targets<textarea className={`${field} font-mono`} rows={4} value={draft.deny.join("\n")} onChange={(event) => update("deny", event.target.value ? event.target.value.split("\n") : [])} /></label>
        <p className="text-xs text-ink-3">One target per line: tool kind, exact/prefix/glob, then the target. Example: bash prefix rm . Empty allowlists permit none.</p>
        <label className="flex flex-col gap-1 text-xs text-ink-3">Name members see<input className={field} maxLength={100} placeholder="your organization" value={draft.org_name} onChange={(event) => update("org_name", event.target.value)} /></label>
        <OrgRules controls={draft.controls} onChange={(controls) => update("controls", controls)} />
      </fieldset>
      <div className="flex flex-wrap gap-s1">
        <button type="submit" className={primary} disabled={locked}>Save approval limits</button>
        {role && <button type="button" className={button} disabled={locked} onClick={() => void save(true)}>Use inherited limits</button>}
        <button type="button" className={button} disabled={busy} onClick={() => void reload()}>Reload policy and keep drafts</button>
      </div>
      {saved && <p role="status" className="text-sm text-pass">Approval limits saved.</p>}
    </form>
    {error && <p role="alert" className={alert}>{error}</p>}
    <p className="text-xs text-ink-3">Organization approval limits govern which approvals members can save. Publishing generic per-tool rules for organization roles is not supported by the account API.</p>
  </section>;
}
