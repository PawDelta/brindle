/* Settings › Organization › Review policies (design/planning-doc-round6.html §3, Enterprise governance):
   one row per rule, what it matches and who it requires, how many designs in review it covers now, and
   the form to add or edit one. Read through the daemon (GET /v1/review-policies: the rules, the teams
   and roles they can name, the designs each covers) and saved as the whole list at the version read
   (PUT /v1/review-policies). Nothing is read until the person opens it. The Library decides who may. */

import { useEffect, useState } from "react";
import { DaemonError, type DaemonClient, type LibraryPerson, type ReviewPoliciesState, type ReviewPolicyRule } from "../../api/client";
import { LoadingButton } from "../../components/LoadingButton";
import { alert, button, field, main, notice, quiet } from "../../components/ui";

type Draft = {
  /* The rule being edited, or null for a new one. */
  editing: string | null;
  name: string;
  kind: ReviewPolicyRule["when"]["kind"];
  paths: string;
  label: string;
  approvals: number;
  /* "team:<id>", "role:<id>" or "person:<sub>". */
  needs: string;
  note: string;
};

const blank: Draft = { editing: null, name: "", kind: "paths", paths: "", label: "", approvals: 1, needs: "", note: "" };

const failureMessage = (failure: unknown) => (failure instanceof Error ? failure.message : String(failure));

const slug = (name: string, taken: string[]) => {
  const base = name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "policy";
  let id = base;
  for (let n = 2; taken.includes(id); n++) id = `${base}-${n}`;
  return id;
};

const initials = (name: string) => name.split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();

function WhenCell({ rule }: { rule: ReviewPolicyRule }) {
  const { kind, paths = [], label } = rule.when;
  return (
    <span>
      {kind === "paths" && <><b className="font-medium text-ink">Touches</b> <code>{paths.join(" or ")}</code></>}
      {kind === "label" && <><b className="font-medium text-ink">Has the label</b> <code>{label}</code></>}
      {kind === "any" && <b className="font-medium text-ink">Every design</b>}
      <small className="block text-xs text-ink-3">{rule.name}</small>
    </span>
  );
}

function NeedsCell({ rule }: { rule: ReviewPolicyRule }) {
  const who = rule.needs.name ?? rule.needs.id;
  const people = rule.eligible == null ? "" : ` · any of ${rule.eligible} people`;
  return (
    <span className="flex items-start gap-2">
      <span aria-hidden className="mt-px grid h-6 w-6 shrink-0 place-items-center rounded-full bg-surface-2 text-[10px] text-ink-2">{initials(who)}</span>
      <span>
        <b className="font-medium text-ink">{rule.needs.kind === "person" ? who : `${rule.approvals} approval${rule.approvals === 1 ? "" : "s"} from ${who}`}</b>
        <small className="block text-xs text-ink-3">
          {rule.needs.kind === "person" ? "Person · must approve personally" : `${rule.needs.kind === "team" ? "Team" : "Role"}${people}`}
        </small>
      </span>
    </span>
  );
}

export function ReviewPolicies({ client }: { client: DaemonClient }) {
  const [open, setOpen] = useState(false);
  const [opened, setOpened] = useState(false);
  return (
    <div className="w-full space-y-s2">
      <button type="button" className={button} aria-expanded={open} onClick={() => { setOpened(true); setOpen((o) => !o); }}>
        {open ? "Hide review policies" : "Review policies"}
      </button>
      {opened && <div hidden={!open}><Policies client={client} /></div>}
    </div>
  );
}

function Policies({ client }: { client: DaemonClient }) {
  const [state, setState] = useState<ReviewPoliciesState | null>(null);
  const [people, setPeople] = useState<LibraryPerson[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(blank);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    setError(null);
    try {
      const [read, directory] = await Promise.all([client.reviewPolicies(), client.libraryPeople().catch(() => ({ people: [] as LibraryPerson[] }))]);
      setState(read);
      setPeople(directory.people);
    } catch (failure) {
      setError(failureMessage(failure));
    }
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps -- read once when opened; Reload is the button
  useEffect(() => { void load(); }, [client]);

  if (!state) {
    return (
      <section className="space-y-s2" aria-label="Review policies">
        {error ? <><p role="alert" className={alert}>{error}</p><button type="button" className={button} onClick={() => void load()}>Load review policies</button></>
          : <p role="status" className="text-sm text-ink-3">Loading review policies…</p>}
      </section>
    );
  }

  const covered = (policy: string) => state.in_review.policies.find((p) => p.policy === policy);
  const rules = state.policies;
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((d) => ({ ...d, [key]: value }));
  const person = draft.needs.startsWith("person:");
  const paths = draft.paths.split(",").map((p) => p.trim()).filter(Boolean);
  const valid = !!draft.name.trim() && !!draft.needs && (draft.kind === "any" || (draft.kind === "paths" ? paths.length > 0 : !!draft.label.trim()));
  // What saving would apply to: only a rule already saved has a count to show.
  const applies = draft.editing ? covered(draft.editing) : undefined;

  const edit = (rule: ReviewPolicyRule) => setDraft({
    editing: rule.id, name: rule.name, kind: rule.when.kind, paths: (rule.when.paths ?? []).join(", "), label: rule.when.label ?? "",
    approvals: rule.approvals, needs: `${rule.needs.kind}:${rule.needs.id}`, note: rule.note ?? "",
  });

  const save = async () => {
    if (!valid || busy) return;
    const [kind, id] = [draft.needs.slice(0, draft.needs.indexOf(":")), draft.needs.slice(draft.needs.indexOf(":") + 1)] as [ReviewPolicyRule["needs"]["kind"], string];
    const next: ReviewPolicyRule = {
      ...rules.find((r) => r.id === draft.editing),
      id: draft.editing ?? slug(draft.name, rules.map((r) => r.id)),
      name: draft.name.trim(),
      when: draft.kind === "paths" ? { kind: "paths", paths } : draft.kind === "label" ? { kind: "label", label: draft.label.trim() } : { kind: "any" },
      needs: { kind, id },
      approvals: kind === "person" ? 1 : draft.approvals,
      note: draft.note.trim() || null,
    };
    setBusy(true);
    setError(null);
    try {
      await client.saveReviewPolicies(state.version, draft.editing ? rules.map((r) => (r.id === draft.editing ? next : r)) : [...rules, next]);
      setDraft(blank);
      await load();
    } catch (failure) {
      // A stale version means someone saved first: read again before offering another save.
      setError(failureMessage(failure));
      if (failure instanceof DaemonError && failure.status === 409) await load();
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="space-y-s3" aria-label="Review policies" data-testid="review-policies">
      <div className="space-y-1">
        <h2 className="text-base font-semibold">Review policies</h2>
        <p className="text-sm text-ink-2">Who must approve a design before brindle builds it. Members can add reviewers on top, but can't remove these. A policy is checked against the files a design says it will change, and again on every revision.</p>
      </div>
      <div role="table" aria-label="Policies">
        <div role="row" className="grid grid-cols-[minmax(0,1.35fr)_minmax(0,1.25fr)_minmax(0,.9fr)_64px] gap-s2 pb-1 text-[11px] font-medium uppercase tracking-wider text-ink-3">
          <span role="columnheader">When a design</span><span role="columnheader">It needs</span><span role="columnheader">Applies now</span><span role="columnheader" />
        </div>
        {rules.map((rule) => {
          const n = covered(rule.id)?.count ?? 0;
          return (
            <div key={rule.id} role="row" data-testid={`review-policy-${rule.id}`} data-count={n} className="grid grid-cols-[minmax(0,1.35fr)_minmax(0,1.25fr)_minmax(0,.9fr)_64px] items-center gap-s2 border-t border-line py-s1 text-sm text-ink-2">
              <WhenCell rule={rule} />
              <NeedsCell rule={rule} />
              <span>{n === 0 ? "None right now" : `${n} design${n === 1 ? "" : "s"} in review`}</span>
              <button type="button" className={`${quiet} justify-self-end`} aria-label={`Edit ${rule.name}`} onClick={() => edit(rule)}>Edit</button>
            </div>
          );
        })}
        {rules.length === 0 && <p className="border-t border-line py-s1 text-sm text-ink-3">No policies yet.</p>}
      </div>
      {state.in_review.truncated && <p className="text-xs text-ink-3">Counts cover the newest designs you can open.</p>}

      <form className="grid gap-s2 border-t border-branch pt-s2 text-sm text-ink-2" aria-label={draft.editing ? "Edit policy" : "New policy"} onSubmit={(e) => { e.preventDefault(); void save(); }}>
        <span className="text-xs font-medium uppercase tracking-wider text-branch">{draft.editing ? "Edit policy" : "New policy"}</span>
        <label className="grid gap-1 sm:grid-cols-[118px_minmax(0,1fr)] sm:items-center"><span className="text-ink-3">Name</span>
          <input className={field} value={draft.name} onChange={(e) => set("name", e.target.value)} />
        </label>
        <div className="grid gap-1 sm:grid-cols-[118px_minmax(0,1fr)] sm:items-center"><span className="text-ink-3">When a design</span>
          <div className="flex flex-wrap items-center gap-2">
            <select className={field} aria-label="When a design" value={draft.kind} onChange={(e) => set("kind", e.target.value as Draft["kind"])}>
              <option value="paths">Touches paths</option><option value="label">Has a label</option><option value="any">Is any design</option>
            </select>
            {draft.kind === "paths" && <input className={`${field} min-w-52 flex-1 font-mono`} aria-label="Paths" placeholder="payments/**, billing/ledger/**" value={draft.paths} onChange={(e) => set("paths", e.target.value)} />}
            {draft.kind === "label" && <input className={`${field} min-w-52 flex-1 font-mono`} aria-label="Label" value={draft.label} onChange={(e) => set("label", e.target.value)} />}
          </div>
        </div>
        <div className="grid gap-1 sm:grid-cols-[118px_minmax(0,1fr)] sm:items-center"><span className="text-ink-3">It needs</span>
          <div className="flex flex-wrap items-center gap-2">
            <input type="number" min={1} max={20} className={`${field} w-16 text-center`} aria-label="Approvals" disabled={person} value={person ? 1 : draft.approvals} onChange={(e) => set("approvals", Math.min(20, Math.max(1, Number(e.target.value) || 1)))} />
            <span>approval from</span>
            <select className={`${field} min-w-52 flex-1`} aria-label="Approvers" value={draft.needs} onChange={(e) => set("needs", e.target.value)}>
              <option value="">Choose who…</option>
              <optgroup label="Teams">{state.directory.teams.map((t) => <option key={t.id} value={`team:${t.id}`}>{t.name}</option>)}</optgroup>
              <optgroup label="Roles">{state.directory.roles.map((r) => <option key={r.id} value={`role:${r.id}`}>{r.name}</option>)}</optgroup>
              <optgroup label="People">{people.map((p) => <option key={p.sub} value={`person:${p.sub}`}>{p.email || p.sub}</option>)}</optgroup>
            </select>
          </div>
        </div>
        <label className="grid gap-1 sm:grid-cols-[118px_minmax(0,1fr)] sm:items-center"><span className="text-ink-3">Members see</span>
          <input className={field} value={draft.note} onChange={(e) => set("note", e.target.value)} />
        </label>
        {applies && applies.count > 0 && (
          <p className={notice} data-testid="review-policy-applies" data-count={applies.count}>
            <b className="font-medium">{`Saving applies it to ${applies.count} design${applies.count === 1 ? "" : "s"} in review now`}</b>
            {` (${applies.designs.map((d) => d.title).join(", ")}${applies.count > applies.designs.length ? ", …" : ""}). Designs already building aren't stopped.`}
          </p>
        )}
        {error && <p role="alert" className={alert}>{error}</p>}
        <div className="flex items-center justify-end gap-s1">
          <span className="mr-auto text-xs text-ink-3">Every policy change is recorded with who made it and when.</span>
          <button type="button" className={quiet} onClick={() => { setDraft(blank); setError(null); }}>Cancel</button>
          <LoadingButton type="submit" className={main} disabled={!valid || busy}>Save policy</LoadingButton>
        </div>
      </form>
    </section>
  );
}
