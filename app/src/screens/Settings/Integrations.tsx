/* Settings › Integrations (library-redesign option A, state 10): connect Azure DevOps and Jira for the
   organization, map each repo to a project, pick the item types and done states, and say what brindle
   may do there. Shown to org owners and admins only (GET /account's `role`; the Library refuses anyone
   else too). Connecting is a browser sign-in on the Library's site (the provider redirects back to the
   browser that started it), so Connect opens that page; Test and Disconnect and the settings are the
   app's. brindle never holds a tracker token here: the Library seals it. */

import { useEffect, useState } from "react";
import type { DaemonClient, TrackerIntegrations, TrackerName, TrackerSettings } from "../../api/client";
import { openExternal, type OpenUrl } from "../../api/open";
import { plain } from "../../api/wording";
import { alert, button, card, field, h2, label, notice } from "../../components/ui";

export type IntegrationsClient = Pick<DaemonClient, "account" | "trackerIntegrations" | "saveTrackerSettings" | "testTracker" | "disconnectTracker">;

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

const NAMES: Record<TrackerName, string> = { jira: "Jira", azure_devops: "Azure DevOps" };
const TYPES: Record<TrackerName, { design: string; task: string; passage: string }> = {
  jira: { design: "Epic", task: "Story", passage: "Task" },
  azure_devops: { design: "Epic", task: "User Story", passage: "Task" },
};
const DONE: Record<TrackerName, string[]> = { jira: ["Done"], azure_devops: ["Resolved", "Closed"] };
const WHO: [TrackerSettings["who_can_create"], string][] = [
  ["authors_and_reviewers", "A design's author and reviewers"],
  ["editors", "Anyone who can edit the design"],
  ["members", "Any member of the organization"],
  ["admins", "Org admins only"],
];
const ALLOW: [keyof TrackerSettings["allow"], string][] = [
  ["create_items", "Create items from a design's tasks and passages"],
  ["link_back", "Add a link to the design on each item it creates"],
  ["close_on_merge", "Close a story when its task's branch merges"],
  ["include_text", "Include the design's text in created items (off: title, files and a link only)"],
];

const when = (v?: number | null) => (v ? new Date(v * 1000).toLocaleDateString() : null);

function complete(s: Partial<TrackerSettings> | undefined, connected: TrackerName[]): TrackerSettings {
  const types: TrackerSettings["types"] = {};
  const done: TrackerSettings["done_states"] = {};
  for (const t of connected) {
    types[t] = s?.types?.[t] ?? TYPES[t];
    done[t] = s?.done_states?.[t] ?? DONE[t];
  }
  return {
    repos: s?.repos ?? [],
    types,
    done_states: done,
    allow: { create_items: true, link_back: true, close_on_merge: false, include_text: false, ...s?.allow },
    who_can_create: s?.who_can_create ?? "authors_and_reviewers",
  };
}

export function Integrations({ client, open = openExternal }: { client: IntegrationsClient; open?: OpenUrl }) {
  const [admin, setAdmin] = useState(false);
  const [state, setState] = useState<TrackerIntegrations | null>(null);
  const [draft, setDraft] = useState<TrackerSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = (live: () => boolean) =>
    client.trackerIntegrations().then((got) => {
      if (!live()) return;
      setState(got);
      setDraft(complete(got.settings, (Object.keys(got.trackers) as TrackerName[]).filter((t) => got.trackers[t].connected)));
    }, (e) => live() && setError(message(e)));

  useEffect(() => {
    let live = true;
    client.account().then(
      (a) => {
        if (!live || (a.role !== "owner" && a.role !== "admin")) return;
        setAdmin(true);
        void load(() => live);
      },
      () => undefined,
    );
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per client
  }, [client]);

  if (!admin) return null;

  const run = async (what: () => Promise<string>, reload = false) => {
    setBusy(true);
    setError(null);
    setSaid(null);
    try {
      setSaid(await what());
      if (reload) await load(() => true);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };

  const trackers = state ? (Object.keys(state.trackers) as TrackerName[]) : [];
  const connected = trackers.filter((t) => state?.trackers[t].connected);
  const mapping = (key: string) => draft?.repos.find((r) => r.repo_key === key);
  const mapRepo = (r: { repo: string; repo_key: string }, patch: Partial<TrackerSettings["repos"][number]> | null) =>
    setDraft((d) => d && {
      ...d,
      repos: patch === null ? d.repos.filter((m) => m.repo_key !== r.repo_key)
        : d.repos.some((m) => m.repo_key === r.repo_key) ? d.repos.map((m) => (m.repo_key === r.repo_key ? { ...m, ...patch } : m))
        : [...d.repos, { repo: r.repo, repo_key: r.repo_key, tracker: connected[0], project: "", ...patch }],
    });

  return (
    <section id="integrations" aria-labelledby="integrations-title" className="flex scroll-mt-s2 flex-col gap-s1" data-testid="integrations">
      <header className="flex flex-wrap items-baseline gap-x-s2">
        <h2 id="integrations-title" className={h2}>Integrations</h2>
        <span className="text-xs text-ink-3">Work trackers for your organization's designs. Members see linked items; only admins change what is here.</span>
      </header>
      {trackers.map((t) => {
        const c = state!.trackers[t];
        return (
          <div key={t} className={`${card} flex flex-col gap-1 px-s2 py-s1 text-sm`} data-tracker={t} data-connected={c.connected}>
            <div className="flex flex-wrap items-center justify-between gap-s1">
              <span><strong>{NAMES[t]}</strong> <span className="text-ink-3">{c.connected ? `Connected · ${[c.site, c.connected_by && `by ${c.connected_by}`, when(c.connected_at)].filter(Boolean).join(" · ")}` : c.configured ? "Not connected" : "Not available on this server yet"}</span></span>
              <span className="flex gap-1">
                {c.connected && <button type="button" className={button} disabled={busy} onClick={() => void run(async () => { await client.testTracker(t); return `${NAMES[t]} answered.`; }, true)}>Test</button>}
                {c.connected && <button type="button" className={button} disabled={busy} onClick={() => void run(async () => { await client.disconnectTracker(t); return `${NAMES[t]} is disconnected. Its sign-in is deleted here; revoke brindle in ${NAMES[t]}'s own app list too.`; }, true)}>Disconnect</button>}
                {c.configured && <button type="button" className={button} disabled={busy} onClick={() => void open(state!.connect_url)}>{c.connected ? "Reconnect…" : `Connect ${NAMES[t]}…`}</button>}
              </span>
            </div>
            {c.last_error && <span className="text-xs text-sap">{`${NAMES[t]} refused brindle's connection (${c.last_error.error}). Reconnect it.`}</span>}
          </div>
        );
      })}
      {draft && connected.length > 0 && (
        <div className={`${card} flex flex-col gap-s1 px-s2 py-s2 text-sm`} role="group" aria-label="What brindle may do">
          <div className="flex flex-col gap-1">
            <span className={label}>Repos and projects</span>
            {state!.repos.length === 0 && <span className="text-xs text-ink-3">No repo on this machine can be mapped yet.</span>}
            {state!.repos.map((r) => {
              const m = mapping(r.repo_key);
              return (
                <div key={r.repo_key} className="flex flex-wrap items-center gap-1" data-repo={r.repo}>
                  <span className="w-40 truncate font-mono text-xs">{r.repo}</span>
                  <select className={field} aria-label={`${r.repo} tracker`} value={m?.tracker ?? ""} onChange={(e) => mapRepo(r, e.target.value ? { tracker: e.target.value as TrackerName } : null)}>
                    <option value="">Not mapped</option>
                    {connected.map((t) => <option key={t} value={t}>{NAMES[t]}</option>)}
                  </select>
                  {m && <input className={field} aria-label={`${r.repo} project`} placeholder={m.tracker === "jira" ? "Project key (BRN)" : "Project"} value={m.project} onChange={(e) => mapRepo(r, { project: e.target.value })} />}
                  {m?.tracker === "azure_devops" && <input className={field} aria-label={`${r.repo} area`} placeholder="Area path (optional)" value={m.area ?? ""} onChange={(e) => mapRepo(r, { area: e.target.value || null })} />}
                </div>
              );
            })}
          </div>
          {connected.map((t) => (
            <div key={t} className="flex flex-col gap-1" data-types={t}>
              <span className={label}>{`${NAMES[t]} item types`}</span>
              <div className="flex flex-wrap items-center gap-1">
                {(["design", "task", "passage"] as const).map((k) => (
                  <label key={k} className="flex items-center gap-1 text-xs">{k === "design" ? "A design" : k === "task" ? "A task of its split" : "A passage"} →
                    <input className={`${field} w-28`} aria-label={`${NAMES[t]} ${k} type`} value={draft.types[t]?.[k] ?? ""} onChange={(e) => setDraft({ ...draft, types: { ...draft.types, [t]: { ...(draft.types[t] ?? TYPES[t]), [k]: e.target.value } } })} />
                  </label>
                ))}
              </div>
              <label className="flex items-center gap-1 text-xs">Counted as done:
                <input className={field} aria-label={`${NAMES[t]} done states`} value={(draft.done_states[t] ?? []).join(", ")} onChange={(e) => setDraft({ ...draft, done_states: { ...draft.done_states, [t]: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) } })} />
              </label>
            </div>
          ))}
          <div className="flex flex-col gap-1">
            <span className={label}>brindle may</span>
            <label className="flex items-center gap-2"><input type="checkbox" checked disabled /> Read the state of linked items <span className="text-xs text-ink-3">always, while connected</span></label>
            {ALLOW.map(([k, text]) => (
              <label key={k} className="flex items-center gap-2"><input type="checkbox" checked={draft.allow[k]} onChange={(e) => setDraft({ ...draft, allow: { ...draft.allow, [k]: e.target.checked } })} /> {text}</label>
            ))}
          </div>
          <label className="flex flex-col gap-0.5">
            <span className={label}>Who can create items</span>
            <select className={field} value={draft.who_can_create} onChange={(e) => setDraft({ ...draft, who_can_create: e.target.value as TrackerSettings["who_can_create"] })}>
              {WHO.map(([v, text]) => <option key={v} value={v}>{text}</option>)}
            </select>
          </label>
          <div>
            <button type="button" className={button} disabled={busy} onClick={() => void run(async () => { await client.saveTrackerSettings(draft); return "Saved."; }, true)}>Save</button>
          </div>
        </div>
      )}
      {said && <p role="status" className={notice}>{said}</p>}
      {error && <p role="alert" className={alert}>{error}</p>}
    </section>
  );
}
