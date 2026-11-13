/* Settings › Organization › Review policies (Enterprise governance):
   one row per rule, what it matches and who it requires, how many designs in review it covers now, and
   the form to add or edit one. Read through the daemon (GET /v1/review-policies: the rules, the teams
   and roles they can name, the designs each covers) and saved as the whole list at the version read
   (PUT /v1/review-policies). Nothing is read until the person opens it. The Library decides who may. */

import { Fragment, useEffect, useRef, useState } from "react";
import { DaemonError, type DaemonClient, type DraftImpact, type LibraryPerson, type ReviewPoliciesState, type ReviewPolicyRule } from "../../api/client";
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

/* The Library's limit on a rule id. */
const MAX_ID = 32;

const slug = (name: string, taken: string[]) => {
  const base = name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "policy";
  let id = base.slice(0, MAX_ID).replace(/-+$/, "");
  for (let n = 2; taken.includes(id); n++) {
    const suffix = `-${n}`;
    id = `${base.slice(0, MAX_ID - suffix.length).replace(/-+$/, "")}${suffix}`;
  }
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
  /* Another admin saved (or the org changed) since this was read: saving waits for a reload. */
  const [stale, setStale] = useState(false);
  /* The rule whose removal is awaiting confirmation in its row, bound to the org it was opened under, and
     what the list without it would cover (result null when that count failed). */
  const [removing, setRemoving] = useState<{ id: string; org: string; opening: number } | null>(null);
  /* Each opening of a removal gets its own number, so a count from an earlier opening never answers this one. */
  const openings = useRef(0);
  const [removeImpact, setRemoveImpact] = useState<{ key: string; result: DraftImpact | null } | null>(null);

  /* Reading again keeps the draft, unless it was written for another org's rules. */
  /* Reads can overlap (Reload while a save's reload is out): the newest one started wins and an older
     answer is dropped, and whether the draft is another org's is decided against the org last accepted,
     not the one the request began under. */
  const reads = useRef({ latest: 0, org: null as string | null });
  const load = async () => {
    const mine = ++reads.current.latest;
    setError(null);
    try {
      const [read, directory] = await Promise.all([client.reviewPolicies(), client.libraryPeople().catch(() => ({ people: [] as LibraryPerson[], me: undefined as string | undefined }))]);
      if (mine !== reads.current.latest) return;
      const another = reads.current.org !== null && reads.current.org !== read.org_id;
      reads.current.org = read.org_id;
      if (another) { setDraft(blank); setRemoving(null); setRemoveImpact(null); }
      setState(read);
      setPeople([...(directory.me ? [{ sub: directory.me, email: "You" }] : []), ...directory.people]);
      setStale(false);
    } catch (failure) {
      if (mine === reads.current.latest) setError(failureMessage(failure));
    }
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps -- read once when opened; Reload is the button
  useEffect(() => { void load(); }, [client]);

  /* The rule the form describes, or null while it isn't complete. */
  const ruleOf = (d: Draft): ReviewPolicyRule | null => {
    if (!state) return null;
    const rules = state.policies;
    const list = d.paths.split(",").map((p) => p.trim()).filter(Boolean);
    const complete = !!d.name.trim() && !!d.needs && (d.kind === "any" || (d.kind === "paths" ? list.length > 0 : !!d.label.trim()));
    if (!complete || (d.editing && !rules.some((r) => r.id === d.editing))) return null;
    const kind = d.needs.slice(0, d.needs.indexOf(":")) as ReviewPolicyRule["needs"]["kind"];
    return {
      ...rules.find((r) => r.id === d.editing),
      id: d.editing ?? slug(d.name, rules.map((r) => r.id)),
      name: d.name.trim(),
      when: d.kind === "paths" ? { kind: "paths", paths: list } : d.kind === "label" ? { kind: "label", label: d.label.trim() } : { kind: "any" },
      needs: { kind, id: d.needs.slice(d.needs.indexOf(":") + 1) },
      approvals: kind === "person" ? 1 : d.approvals,
      note: d.note.trim() || null,
    };
  };
  /* The whole list as saving would leave it. */
  const candidate = (d: Draft): ReviewPolicyRule[] | null => {
    const next = ruleOf(d);
    if (!state || !next) return null;
    return d.editing ? state.policies.map((r) => (r.id === d.editing ? next : r)) : [...state.policies, next];
  };

  /* What saving would do to the designs in review, asked of the Library once the person leaves a field
     (not on every key). The answer is of one org's rules and one draft: it is shown only while both are
     still what's on the form, so a slow reply never describes a draft since changed. */
  const [settled, setSettled] = useState<Draft>(blank);
  useEffect(() => setSettled(draft), [draft.kind, draft.needs, draft.approvals, draft.editing]); // eslint-disable-line react-hooks/exhaustive-deps
  // The rule alone: the Library counts designs any listed rule covers, so the rest of the list would be counted too.
  const asking = (r => (r ? [r] : null))(ruleOf(settled));
  const askKey = state && asking ? JSON.stringify([state.org_id, asking]) : null;
  const [impact, setImpact] = useState<{ key: string; result: DraftImpact } | null>(null);
  useEffect(() => {
    if (!state || !asking || !askKey) return;
    let live = true;
    client.reviewPolicyImpact(state.org_id, asking).then((result) => { if (live) setImpact({ key: askKey, result }); }, () => { if (live) setImpact(null); });
    return () => { live = false; };
  // eslint-disable-next-line react-hooks/exhaustive-deps -- askKey is the whole of asking and state.org_id
  }, [client, askKey]);
  const drafted = ruleOf(draft);
  const shownKey = state && drafted ? JSON.stringify([state.org_id, [drafted]]) : null;
  const shown = impact && impact.key === shownKey ? impact.result : null;

  /* Removing a rule asks for the whole list without it, of the org loaded, once per confirmation. */
  const removeKey = state && removing && removing.org === state.org_id
    ? JSON.stringify([state.org_id, removing.id, removing.opening, state.policies.filter((r) => r.id !== removing.id)]) : null;
  useEffect(() => {
    if (!state || !removing || !removeKey) return;
    let live = true;
    client.reviewPolicyImpact(state.org_id, state.policies.filter((r) => r.id !== removing.id)).then((result) => { if (live) setRemoveImpact({ key: removeKey, result }); }, () => { if (live) setRemoveImpact({ key: removeKey, result: null }); });
    return () => { live = false; };
  // eslint-disable-next-line react-hooks/exhaustive-deps -- removeKey is the whole of the request
  }, [client, removeKey]);
  /* Null while the count is pending or failed: Remove is only offered once the count is shown. */
  const removalAnswer = removeImpact && removeImpact.key === removeKey ? removeImpact : null;
  const removal = removalAnswer?.result ?? null;

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
  const valid = candidate(draft) !== null;

  const edit = (rule: ReviewPolicyRule) => setDraft({
    editing: rule.id, name: rule.name, kind: rule.when.kind, paths: (rule.when.paths ?? []).join(", "), label: rule.when.label ?? "",
    approvals: rule.approvals, needs: `${rule.needs.kind}:${rule.needs.id}`, note: rule.note ?? "",
  });

  /* The whole list, saved at the version read. */
  const write = async (list: ReviewPolicyRule[], saved: () => void) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await client.saveReviewPolicies(state.org_id, state.version, list);
      saved();
      await load();
    } catch (failure) {
      // A 409 is another admin's save, or another active org: nothing was written, and the draft stays
      // until the person reads what changed and saves again.
      setError(failureMessage(failure));
      if (failure instanceof DaemonError && failure.status === 409) setStale(true);
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    const list = candidate(draft);
    if (!list) return;
    await write(list, () => setDraft(blank));
  };

  /* Only a removal whose count is shown, opened under the org still loaded, can be saved. */
  const remove = (rule: ReviewPolicyRule) => {
    if (!removal || removing?.id !== rule.id || removing.org !== state.org_id) return;
    void write(rules.filter((r) => r.id !== rule.id), () => setRemoving(null));
  };

  return (
    <section className="space-y-s3" aria-label="Review policies" data-testid="review-policies">
      <div className="space-y-1">
        <h2 className="text-base font-semibold">Review policies</h2>
        <p className="text-sm text-ink-2">Who must approve a design before brindle builds it. Members can add reviewers on top, but can't remove these. A policy is checked against the files a design says it will change, and again on every revision.</p>
      </div>
      <div role="table" aria-label="Policies">
        <div role="row" className="grid grid-cols-[minmax(0,1.35fr)_minmax(0,1.25fr)_minmax(0,.9fr)_max-content] gap-s2 pb-1 text-[11px] font-medium uppercase tracking-wider text-ink-3">
          <span role="columnheader">When a design</span><span role="columnheader">It needs</span><span role="columnheader">Applies now</span><span role="columnheader" />
        </div>
        {rules.map((rule) => {
          const n = covered(rule.id)?.count ?? 0;
          return (
            <Fragment key={rule.id}>
              <div role="row" data-testid={`review-policy-${rule.id}`} data-count={n} className="grid grid-cols-[minmax(0,1.35fr)_minmax(0,1.25fr)_minmax(0,.9fr)_max-content] items-center gap-s2 border-t border-line py-s1 text-sm text-ink-2">
                <WhenCell rule={rule} />
                <NeedsCell rule={rule} />
                <span>{n === 0 ? "None right now" : `${n} design${n === 1 ? "" : "s"} in review`}</span>
                <span className="flex justify-self-end gap-s1">
                  <button type="button" className={quiet} aria-label={`Edit ${rule.name}`} onClick={() => edit(rule)}>Edit</button>
                  <button type="button" className={quiet} aria-label={`Remove ${rule.name}`} onClick={() => setRemoving({ id: rule.id, org: state.org_id, opening: ++openings.current })}>Remove</button>
                </span>
              </div>
              {removing?.id === rule.id && (
                <div className="space-y-1 border-b border-line pb-s1 text-sm text-ink-2" data-testid={`review-policy-remove-${rule.id}`}>
                  <p>{`Remove ${rule.name}?`}</p>
                  {!removalAnswer && <p role="status" className="text-xs text-ink-3">Counting the designs the rest would cover…</p>}
                  {removalAnswer && !removalAnswer.result && <p role="alert" className={alert}>Couldn't count the designs, so nothing can be removed yet. Keep it, then choose Remove again.</p>}
                  {removal && (
                    <p className={notice} data-testid="review-policy-remove-applies" data-count={removal.count} data-truncated={removal.truncated || undefined}>
                      {`Without it, ${removal.truncated ? "at least " : ""}${removal.count} design${removal.count === 1 ? "" : "s"} in review ${removal.count === 1 ? "is" : "are"} still covered by the policies left`}
                    </p>
                  )}
                  <div className="flex justify-end gap-s1">
                    <button type="button" className={quiet} onClick={() => setRemoving(null)}>Keep</button>
                    <button type="button" className={quiet} aria-label={`Confirm remove ${rule.name}`} disabled={!removal || busy || stale} onClick={() => remove(rule)}>Confirm remove</button>
                  </div>
                </div>
              )}
            </Fragment>
          );
        })}
        {rules.length === 0 && <p className="border-t border-line py-s1 text-sm text-ink-3">No policies yet.</p>}
      </div>
      {state.in_review.truncated && <p className="text-xs text-ink-3">Counts cover the newest designs you can open.</p>}

      <form className="grid gap-s2 border-t border-branch pt-s2 text-sm text-ink-2" aria-label={draft.editing ? "Edit policy" : "New policy"} onBlur={() => setSettled(draft)} onSubmit={(e) => { e.preventDefault(); void save(); }}>
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
        {shown && (
          <p className={notice} data-testid="review-policy-applies" data-count={shown.count} data-truncated={shown.truncated || undefined}>
            <b className="font-medium">{`Saving applies it to ${shown.truncated ? "at least " : ""}${shown.count} design${shown.count === 1 ? "" : "s"} in review now`}</b>
            {shown.designs.length > 0 && ` (${shown.designs.map((d) => d.title).join(", ")}${shown.count > shown.designs.length ? ", …" : ""})`}
            {shown.reopened > 0 && `. ${shown.reopened} that ${shown.reopened === 1 ? "is" : "are"} ready now would stop being ready until the new reviewers sign off`}
            {". Only designs you can open are counted; designs already building aren't stopped."}
          </p>
        )}
        {error && <p role="alert" className={alert}>{error}</p>}
        <div className="flex items-center justify-end gap-s1">
          <span className="mr-auto text-xs text-ink-3">Every policy change is recorded with who made it and when.</span>
          <button type="button" className={quiet} disabled={busy} onClick={() => void load()}>{stale ? "Reload policies and keep my draft" : "Reload policies"}</button>
          <button type="button" className={quiet} onClick={() => { setDraft(blank); setError(null); }}>Cancel</button>
          <LoadingButton type="submit" className={main} disabled={!valid || busy || stale}>Save policy</LoadingButton>
        </div>
      </form>
    </section>
  );
}
