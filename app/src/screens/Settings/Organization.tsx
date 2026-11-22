import { useEffect, useRef, useState } from "react";
import { DaemonError, type DaemonClient, type OrgSettingName, type OrgSettings } from "../../api/client";
import { plain } from "../../api/wording";
import { alert, field, h2, label, main, quiet } from "../../components/ui";

const ROWS: { id: OrgSettingName; name: string; help: string }[] = [
  { id: "share", name: "Sharing", help: "Whether plans can be shared with people, teams and everyone here. People choose who, plan by plan." },
  { id: "share_links", name: "Plan links", help: "Who a plan's link can open for." },
  { id: "prompt_analysis", name: "Prompt analysis", help: "A record of which plans people's prompts matched. Never the prompt itself." },
  { id: "store", name: "Where plans are kept", help: "On each person's Mac, or encrypted on brindle servers." },
  { id: "assist", name: "Briefs", help: "New work starts with a brief from the plans that match it." },
  { id: "import_upload", name: "Imported folders", help: "Folders people add to the Library are uploaded, so briefs can draw on them." },
];

function valueOf(state: OrgSettings, id: OrgSettingName) {
  return id === "share_links" ? state.policy.share_links ?? "org" : state.policy.switches[id] ?? "off";
}
function fixed(state: OrgSettings, id: OrgSettingName) {
  return id === "share_links" || valueOf(state, id).startsWith("enforced_");
}
function choices(id: OrgSettingName) {
  if (id === "share_links") return [
    { value: "org", name: "People in this organization", help: "A link requires sign-in to this organization." },
    { value: "anyone", name: "Anyone with the link", help: "Anyone who has the link can open the plan." },
    { value: "off", name: "Off", help: "Plans cannot be opened through share links." },
  ];
  if (id === "store") return [
    { value: "on", name: "brindle servers", help: "Encrypted in each person's Library, available wherever they sign in." },
    { value: "off", name: "Each person's Mac", help: "Nothing is uploaded. A shared plan is sent on its own." },
  ];
  return [
    { value: "on", name: "On", help: id === "share" ? "Plans can be shared with people, teams and the organization." : "Enabled for members with their Library on brindle servers." },
    { value: "off", name: "Off", help: id === "share" ? "Sharing is turned off." : "Turned off for members." },
  ];
}
function display(state: OrgSettings, id: OrgSettingName) {
  const raw = valueOf(state, id);
  const on = raw === "default_on" || raw === "enforced_on";
  const value = id === "share_links" ? choices(id).find(c => c.value === raw)!.name
    : id === "store" ? on ? "brindle servers" : "Each person's Mac"
    : id === "share" && on ? "People, teams and everyone here" : on ? "On" : "Off";
  return fixed(state, id) ? value : `Starts ${value.toLowerCase()}`;
}

export function Organization({ client }: { client: Pick<DaemonClient, "orgSettings" | "setOrgSetting"> }) {
  const [state, setState] = useState<OrgSettings | null>(null);
  const [editing, setEditing] = useState<OrgSettingName | null>(null);
  const [selected, setSelected] = useState("off");
  const [membersChoose, setMembersChoose] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [scope, setScope] = useState("");
  const [reload, setReload] = useState(0);
  const generation = useRef(0);
  const saving = useRef(false);
  useEffect(() => {
    const current = ++generation.current;
    setState(null);
    setEditing(null);
    setError(null);
    client.orgSettings().then(got => {
      if (generation.current === current) setState(got);
    }, e => {
      if (generation.current === current) setError(plain(String(e)));
    });
    return () => { generation.current++; };
  }, [client, reload]);

  const organizationSwitches = state?.can_edit_org ? state.policy.switches : {};
  const view = state && scope ? { ...state, policy: { ...state.policy, switches: {
    ...organizationSwitches, ...state.policy.groups[scope]?.switches,
    ...Object.fromEntries(Object.entries(organizationSwitches).filter(([, value]) => value?.startsWith("enforced_"))),
  } } } : state;
  const canEdit = (id: OrgSettingName) => !!state && (scope
    ? state.can_edit_groups && !(state.can_edit_org && state.policy.switches[id as keyof typeof state.policy.switches]?.startsWith("enforced_"))
    : state.can_edit_org);

  const open = (id: OrgSettingName) => {
    if (!view || !canEdit(id) || saving.current) return;
    const raw = valueOf(view, id);
    setSelected(id === "share_links" ? raw : raw === "default_on" || raw === "enforced_on" ? "on" : "off");
    setMembersChoose(!fixed(view, id));
    setEditing(id);
    setError(null);
  };
  const save = async () => {
    if (!state || !editing || saving.current) return;
    const current = generation.current;
    const id = editing;
    const value = id === "share_links" ? selected : membersChoose ? selected === "on" ? "default_on" : "off" : `enforced_${selected}`;
    saving.current = true;
    setBusy(true);
    setError(null);
    try {
      const got = await client.setOrgSetting(state, id, value, scope || undefined);
      if (generation.current === current) {
        setState(got);
        setEditing(null);
      }
    } catch (e) {
      if (generation.current === current) {
        if (e instanceof DaemonError && e.status === 409) {
          setEditing(null);
          try {
            const fresh = await client.orgSettings();
            if (generation.current === current) {
              setState(fresh);
              if (scope && !fresh.policy.groups[scope]) setScope("");
              setError("These settings changed. The current values are shown; open the row to make your change again.");
            }
          } catch (readError) {
            if (generation.current === current) setError(plain(String(readError)));
          }
        } else setError(plain(String(e)));
      }
    } finally {
      saving.current = false;
      if (generation.current === current) setBusy(false);
    }
  };

  return <section className="flex flex-col gap-s2" aria-label="Organization Library settings">
    <header className="flex flex-wrap items-baseline gap-x-s2 gap-y-0.5"><h2 className={h2}>Library</h2><span className="text-xs text-ink-3">What everyone gets, as members read it. Change one setting at a time.</span></header>
    {state && Object.keys(state.policy.groups).length > 0 && <label className="flex items-baseline gap-s2 text-sm">Settings for<select className={field} aria-label="Settings for" value={scope} disabled={busy} onChange={e => { setScope(e.target.value); setEditing(null); setError(null); }}><option value="">Organization</option>{Object.keys(state.policy.groups).map(group => <option key={group} value={group}>{group}</option>)}</select></label>}
    {state && !state.can_edit_org && !scope && <p className="px-s2 text-xs text-ink-3">Organization-wide settings require Enterprise. On Team, admins can change their groups' settings.</p>}
    {!state && !error && <p role="status" className="text-sm text-ink-3">Loading organization settings…</p>}
    {view && [true, false].map(isFixed => <div key={String(isFixed)} className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-baseline gap-s2 px-s2"><h3 className={label}>{isFixed ? "Set for everyone" : "Members choose"}</h3><span className="text-xs text-ink-3">{isFixed ? "Members see the value and can't change it." : "Each person starts with the value shown."}</span></div>
      {ROWS.filter(row => (!scope || row.id !== "share_links") && fixed(view, row.id) === isFixed).map(row => <div key={row.id} data-testid={`org-setting-${row.id}`} data-state={editing === row.id ? "editing" : "reading"} className={`border-t px-s2 py-s1 ${editing === row.id ? "border-branch" : "border-line"}`}>
        {editing === row.id ? <div className="flex flex-col gap-s1">
          <div className="flex items-baseline justify-between gap-s2"><div><h4 className="text-sm font-medium">{row.name}</h4><p className="text-xs text-ink-3">{row.help}</p></div><span className="text-xs font-medium uppercase tracking-wider text-branch">Changing</span></div>
          <fieldset disabled={busy} className="flex flex-col gap-1.5"><legend className="sr-only">{row.name}</legend>{choices(row.id).map(choice => <label key={choice.value} className="flex items-start gap-2.5 text-sm"><input type="radio" name={`org-${row.id}`} value={choice.value} checked={selected === choice.value} onChange={() => setSelected(choice.value)} className="mt-1 accent-branch" /><span>{choice.name}<span className="block text-xs text-ink-3">{choice.help}</span></span></label>)}</fieldset>
          {row.id !== "share_links" && <label className="flex items-center gap-2.5 border-t border-line pt-s1 text-sm"><input type="checkbox" checked={membersChoose} disabled={busy} onChange={e => setMembersChoose(e.target.checked)} />Members can change this<span className="text-ink-3">· unticked, it moves to Set for everyone</span></label>}
          <p className="text-[13px] text-ink-2">{membersChoose && row.id !== "share_links" ? "This becomes the starting value. Members keep their own choices." : row.id === "store" ? `This fixes the Library destination for ${scope ? "this group" : "everyone"}. Existing plans stay where they are; this change does not move them.` : `This applies to ${scope ? "this group" : "everyone"}. Members see the value in Settings and cannot change it.`}</p>
          <div className="flex justify-end gap-s1"><button type="button" className={quiet} disabled={busy} onClick={() => setEditing(null)}>Cancel</button><button type="button" className={main} disabled={busy} onClick={() => void save()}>{busy ? "Saving…" : scope ? "Save for group" : "Save for everyone"}</button></div>
        </div> : <div className="grid grid-cols-[minmax(0,1fr)_minmax(120px,280px)_64px] items-center gap-x-s3"><div><h4 className="text-sm font-medium">{row.name}</h4><p className="text-xs text-ink-3">{row.help}</p></div><span className="text-sm">{display(view, row.id)}</span>{canEdit(row.id) ? <button type="button" className={`${quiet} justify-self-end text-xs`} aria-label={`Change ${row.name}`} disabled={busy} onClick={() => open(row.id)}>Change</button> : <span className="text-xs text-ink-3">{scope ? "Org policy" : "Enterprise"}</span>}</div>}
      </div>)}
    </div>)}
    {error && <div role="alert" className={alert}>{error} <button type="button" className={quiet} disabled={busy} onClick={() => setReload(n => n + 1)}>Reload</button></div>}
    <p className="px-s2 text-xs text-ink-3">Every change is recorded with who made it and when.</p>
  </section>;
}
