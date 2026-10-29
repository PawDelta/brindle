/* Settings › Permissions (design/permissions-settings.html): the person's
   permission policy (GET /permissions).

   Capability groups, the default view: the preset picker
   (PUT /permissions/preset, Autonomous recommended), the ask rate from
   GET /permissions/report, and one row per group (Files, Shell, Git & push,
   Network, MCP) with an Allow / Ask / Deny baseline, expanding to its
   subcategories (Inherit / Allow / Ask / Deny, marked when one overrides its
   group); both are set with PUT /permissions/groups. Saved actions are a count
   with a link.

   Advanced: every rule with its decision and limits, searchable and
   filterable (the preset's own included: each can be edited or
   removed, and a removal can be undone by adding it back), and what a
   request no rule covers does (PATCH /permissions). A rule is edited in a
   slide-out sheet in three steps (the action, its decision, its limits or
   lifetime); before a save the editor checks the live rules for one that
   decides differently on the same requests and offers to keep them, replace
   them or resolve the overlap with exceptions (the stricter decision wins
   where they meet, as the engine's own precedence does).

   Saved actions: the allows and denies saved from the Inbox, each edited in
   a sheet (its rung on the scope ladder, its decision, how long it lasts)
   or deleted, with undo. Activity: what changed, from the policy's own
   timestamps and this screen's changes. */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { DaemonClient, PermissionReport, PermissionDecision, PermissionKind, PermissionLife, PermissionMatchType, PermissionPreset, PermissionRulePut, PermissionUnmatched, SavedActionKind, SavedActionPut } from "../../api/client";
import {
  DECISIONS,
  LEVELS,
  LEVEL_LABEL,
  PLAIN_MATCHES,
  UNITS,
  UNMATCHED,
  conflictsFor,
  kindLabel,
  limitsText,
  presetLabel,
  ruleAction,
  ruleLimits,
  ruleSummary,
  savedLasts,
  savedScopes,
  specificTarget,
  splitMinutes,
  strength,
  toMinutes,
  when,
  type Conflict,
  type RuleShape,
  type Unit,
} from "../../api/permissions";
import type { AskScopeDTO, PermissionPolicyDTO, PermissionRuleDTO, SavedActionDTO } from "../../api/schema";
import { plain } from "../../api/wording";
import { Sheet } from "../../components/Sheet";
import { alert, button, card, field, h2, mono, pill, quiet } from "../../components/ui";

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

type View = "groups" | "advanced" | "saved" | "activity";
const VIEW_TITLE: Record<View, string> = { groups: "Permissions", advanced: "Advanced: all rules", saved: "Saved actions", activity: "Activity" };

/* brindle.permission_groups.GROUPS, as the page names them. */
const GROUP_LIST: { id: string; name: string; subs: [string, string, string][] }[] = [
  { id: "files", name: "Files", subs: [["read", "Read files", "Files in the worktree and elsewhere"], ["write-worktree", "Write and edit in the worktree", "Inside this checkout"], ["write-outside", "Write and edit outside the worktree", "Anywhere else on disk"]] },
  { id: "shell", name: "Shell", subs: [["readonly", "Read-only commands", "grep, ls, cat, head"], ["dev-tools", "Tests & build tools", "npm, pytest, make, cargo"], ["install", "Package installs", "npm install, pip install, uv add"], ["destructive", "Destructive commands", "rm, kill, dd"], ["other", "Other commands", "Anything not listed above"]] },
  { id: "git", name: "Git & push", subs: [["readonly", "Read-only git", "status, diff, log, show"], ["commit", "Commit and branch locally", "commit, branch, switch, merge"], ["push-feature", "Push to feature branches", "Any branch that is not the default or a release"], ["push-protected", "Push to the default or a release branch", "A release push starts a signed build"], ["other", "Other git commands", "reset, clean, rebase, force flags"]] },
  { id: "network", name: "Network", subs: [["fetch", "Fetch URLs", "Web pages and APIs"], ["search", "Web search", "Search the web"]] },
  { id: "mcp", name: "MCP", subs: [["tool", "MCP tool calls", "Tools from connected MCP servers"], ["other", "Other MCP requests", "Resources and prompts"]] },
];
const BASELINE_LEVELS = ["allow", "ask", "deny"] as const;
const LEVEL_NAME: Record<string, string> = { allow: "Allow", ask: "Ask", deny: "Deny", inherit: "Inherit" };
const BY_DEFAULT: Record<string, string> = { allow: "Allowed by default", ask: "Asks by default", deny: "Denied by default" };
/* Which group a rule belongs to, from its kind (a push is a bash rule with the push matcher). */
const isGit = (r: PermissionRuleDTO) => r.match_type === "push" || r.match_type.startsWith("git-") || (r.kind === "bash" && /^git(\s|$)/.test(r.match));
const ruleGroup = (r: PermissionRuleDTO) => (isGit(r) ? "git" : r.kind === "bash" ? "shell" : r.kind === "fetch" ? "network" : r.kind === "mcp" ? "mcp" : ["read", "write", "edit"].includes(r.kind) ? "files" : "other");
interface Feedback {
  text: string;
  failed?: boolean;
  undo?(): void;
}
interface Activity {
  kind: string;
  what: string;
  detail: string;
  at: number;
}

/* The rule editor's actions: a request kind, or a git push (a bash rule with the push matcher). */
const ACTIONS = ["read", "write", "edit", "bash", "push", "fetch", "mcp", "other"];
const MATCH_LABEL: Record<string, string> = { exact: "Is exactly", prefix: "Starts with", glob: "Matches pattern" };
/* The same tones on a rule's pill: in the text only (pills are hairlines, never a fill). */
const INK: Record<string, string> = { deny: "text-sap", ask: "text-branch", allow: "text-pass" };
const fieldLabel = "flex min-w-0 flex-col gap-1 text-xs font-medium text-ink-3";
const iconButton = `${quiet} inline-flex size-8 items-center justify-center p-0`;

const Pencil = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden>
    <path d="m16 3 5 5-12 12-6 1 1-6Z" />
    <path d="m14 5 5 5" />
  </svg>
);
const Bin = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden>
    <path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7" />
  </svg>
);

/* What re-adding a removed rule takes: its fields, with an expiry only while it is still ahead. */
const ruleBody = (r: PermissionRuleDTO) => ({
  kind: r.kind as PermissionKind,
  match: r.match,
  match_type: r.match_type as PermissionMatchType,
  decision: r.decision as PermissionDecision,
  ...(r.label ? { label: r.label } : {}),
  ...(r.limits ? { limits: r.limits } : {}),
  ...(r.expires && r.expires * 1000 > Date.now() ? { expires: r.expires } : {}),
});

export interface PermissionsProps {
  client: DaemonClient;
  /* A rule to open in the editor on arrival (the Inbox's "Open the deny rule"). */
  rule?: string;
  /* A new value opens `rule` again, the page already open. */
  ask?: number;
}

export function Permissions({ client, rule: openId, ask }: PermissionsProps) {
  const [policy, setPolicy] = useState<PermissionPolicyDTO | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [view, setView] = useState<View>("groups");
  const [report, setReport] = useState<PermissionReport | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [log, setLog] = useState<Activity[]>([]);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<PermissionRuleDTO | "new" | null>(null);
  const [savedEditing, setSavedEditing] = useState<SavedActionDTO | null>(null);

  const load = useCallback(
    () =>
      Promise.all([
        client.permissions().then(
          (p) => {
            setPolicy(p);
            setLoadError(null);
          },
          (e: unknown) => setLoadError(message(e)),
        ),
        // The ask-rate summary is context only: without it the page still works.
        client.permissionReport().then(setReport, () => setReport(null)),
      ]),
    [client],
  );
  useEffect(() => {
    void load();
  }, [load]);

  // Opened for one rule: its editor, once, when the policy holding it is in.
  const opened = useRef<string | null>(null);
  useEffect(() => {
    const want = openId ? `${openId}:${ask ?? 0}` : null;
    if (!want || !policy || opened.current === want) return;
    const found = policy.rules.find((r) => r.id === openId);
    if (!found) return;
    opened.current = want;
    setView("advanced");
    setEditing(found);
    document.getElementById("permissions")?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }, [openId, ask, policy]);

  const record = (kind: string, what: string, detail: string) => setLog((l) => [{ kind, what, detail, at: Date.now() / 1000 }, ...l]);

  /* One change on the engine, then the policy read again; what it did (or why not) as the feedback line. */
  const run = async (change: () => Promise<unknown>, done: Feedback) => {
    setBusy(true);
    try {
      await change();
      setFeedback(done);
    } catch (e) {
      setFeedback({ text: message(e), failed: true });
    } finally {
      await load();
      setBusy(false);
    }
  };

  const restoreRule = (r: PermissionRuleDTO) =>
    void run(async () => {
      const back = await client.createPermissionRule(ruleBody(r));
      for (const x of r.exclusions ?? []) await client.addPermissionException(back.id, { kind: x.kind as PermissionKind, match: x.match, match_type: x.match_type as "exact" });
      record("Policy", "Rule restored", `${ruleAction(r)} · ${r.label || r.describe}`);
    }, { text: "Rule restored." });
  const removeRule = (r: PermissionRuleDTO) =>
    void run(async () => {
      await client.deletePermissionRule(r.id);
      record("Policy", "Rule removed", `${ruleAction(r)} · ${r.label || r.describe}`);
    }, { text: `${ruleAction(r)} rule removed. Other matching rules still apply.`, undo: () => restoreRule(r) });

  const restoreSaved = (s: SavedActionDTO) =>
    void run(async () => {
      await client.createSavedAction({
        kind: s.kind as SavedActionKind,
        pattern: s.pattern,
        decision: s.decision as "allow" | "deny",
        scope: s.scope,
        level: (s.level as SavedActionPut["level"]) ?? null,
        request: s.request,
        life: (s.life as PermissionLife) ?? "forever",
        minutes: s.minutes ?? null,
        session: s.session ?? null,
        repo: s.repo ?? null,
        note: s.note,
      });
      record(s.decision === "deny" ? "Deny" : "Approval", "Saved action restored", `${kindLabel(s.kind)} · ${s.pattern}`);
    }, { text: "Restored." });
  const deleteSaved = (s: SavedActionDTO) =>
    void run(async () => {
      await client.deleteSavedAction(s.id);
      record("Delete", "Saved action deleted", `${kindLabel(s.kind)} · ${s.pattern}`);
    }, { text: `${kindLabel(s.kind)} · ${s.pattern} deleted. The next matching request follows your rules again.`, undo: () => restoreSaved(s) });

  return (
    <section id="permissions" aria-labelledby="permissions-title" className="flex scroll-mt-s2 flex-col gap-s1" data-testid="permissions-settings">
      <header className="flex flex-wrap items-baseline gap-x-s2 gap-y-0.5">
        <h2 id="permissions-title" className={h2}>
          Permissions
        </h2>
        <span className="text-xs text-ink-3">What your agents do without asking, what asks you, and what is blocked. When two rules disagree, the deny wins.</span>
      </header>
      {view !== "groups" && (
        <div className="flex items-center gap-s2">
          <button type="button" className={`${quiet} -ml-2`} onClick={() => setView("groups")}>
            ← Back to permissions
          </button>
          <span className="text-sm font-medium">{VIEW_TITLE[view]}</span>
        </div>
      )}
      {loadError && (
        <div role="alert" className={`${alert} flex items-center justify-between gap-s2`}>
          <span>Could not load the permission policy: {loadError}</span>
          <button type="button" className={button} onClick={() => void load()}>
            Retry
          </button>
        </div>
      )}
      {feedback && (
        <p role={feedback.failed ? "alert" : "status"} className={`text-sm ${feedback.failed ? "text-sap" : "text-ink-2"}`} data-testid="permissions-feedback">
          {feedback.text}{" "}
          {feedback.undo && (
            <button type="button" className={`${quiet}`} disabled={busy} onClick={feedback.undo}>
              Undo
            </button>
          )}
        </p>
      )}
      {policy && view === "groups" && <GroupsView client={client} policy={policy} report={report} busy={busy} run={run} record={record} onOpen={setView} />}
      {policy && view === "advanced" && <AdvancedView client={client} policy={policy} busy={busy} run={run} record={record} onEdit={setEditing} onRemove={removeRule} />}
      {policy && view === "saved" && <SavedTab saved={policy.saved} busy={busy} onEdit={setSavedEditing} onDelete={deleteSaved} />}
      {policy && view === "activity" && <ActivityTab policy={policy} log={log} />}
      {policy && editing && (
        <RuleEditor
          key={editing === "new" ? "new" : editing.id}
          client={client}
          rule={editing === "new" ? null : editing}
          rules={policy.rules}
          onClose={() => setEditing(null)}
          onSaved={(text, detail) => {
            setEditing(null);
            record("Policy", text, detail);
            setFeedback({ text: `${text}.` });
            void load();
          }}
        />
      )}
      {policy && savedEditing && (
        <SavedEditor
          key={savedEditing.id}
          client={client}
          saved={savedEditing}
          onClose={() => setSavedEditing(null)}
          onDelete={() => {
            const s = savedEditing;
            setSavedEditing(null);
            deleteSaved(s);
          }}
          onSaved={(s) => {
            setSavedEditing(null);
            record(s.decision === "deny" ? "Deny" : "Approval", "Saved action changed", `${kindLabel(s.kind)} · ${s.decision} · ${s.pattern} · ${savedLasts(s)}`);
            setFeedback({ text: `${kindLabel(s.kind)} updated. Takes effect on the next matching request.` });
            void load();
          }}
        />
      )}
    </section>
  );
}

/* A segmented Allow / Ask / Deny (and Inherit) control: the pressed one is the current answer. */
function Segmented({ label, value, levels, disabled, onPick }: { label: string; value: string; levels: readonly string[]; disabled: boolean; onPick(level: string): void }) {
  return (
    <div role="group" aria-label={label} className="inline-flex overflow-hidden rounded-md border border-line">
      {levels.map((l) => (
        <button
          key={l}
          type="button"
          aria-pressed={value === l}
          disabled={disabled}
          className={`border-l border-line px-s2 py-0.5 text-sm first:border-l-0 ${value === l ? `bg-surface-2 font-medium ${l === "inherit" ? "text-ink" : INK[l]}` : "text-ink-2 hover:bg-surface-2"}`}
          onClick={() => onPick(l)}
        >
          {LEVEL_NAME[l]}
        </button>
      ))}
    </div>
  );
}

/* The default view (design/permissions-settings.html): the preset, how often agents are asked, and one row per
   capability group with its baseline, expanding to the subcategories that may override it. */
function GroupsView({ client, policy, report, busy, run, record, onOpen }: {
  client: DaemonClient;
  policy: PermissionPolicyDTO;
  report: PermissionReport | null;
  busy: boolean;
  run(change: () => Promise<unknown>, done: Feedback): Promise<void>;
  record(kind: string, what: string, detail: string): void;
  onOpen(view: View): void;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const customized = policy.rules.some((r) => r.source !== "preset");
  const stored = new Map((policy.groups ?? []).map((g) => [g.name, g]));
  // A daemon that sent no table leaves every group at the unmatched answer.
  const baselineOf = (id: string) => stored.get(id)?.baseline ?? policy.unmatched;
  const overrideOf = (id: string, sub: string) => stored.get(id)?.subcategories?.[sub] ?? "inherit";

  const usePreset = (name: string) =>
    void run(async () => {
      await client.setPermissionPreset(name as PermissionPreset);
      record("Policy", `${presetLabel(name)} preset chosen`, "Its rules replace the earlier preset's; your own and learned rules stay");
    }, { text: `${presetLabel(name)} loaded. Every rule it added can be edited or removed.` });
  const setBaseline = (g: { id: string; name: string }, level: string) =>
    void run(async () => {
      await client.setPermissionGroups({ [g.id]: { baseline: level } });
      record("Policy", `${g.name} baseline changed`, LEVEL_NAME[level]);
    }, { text: `${g.name} baseline: ${LEVEL_NAME[level]}.` });
  const setOverride = (g: { id: string; name: string }, sub: string, name: string, level: string) =>
    void run(async () => {
      await client.setPermissionGroups({ [g.id]: { subcategories: { [sub]: level } } });
      record("Policy", `${g.name} · ${name} changed`, level === "inherit" ? "Inherits the group" : LEVEL_NAME[level]);
    }, { text: `${name}: ${level === "inherit" ? `inherits ${LEVEL_NAME[baselineOf(g.id)]}` : LEVEL_NAME[level]}.` });

  return (
    <div className="flex flex-col gap-s2">
      <div className={`${card} flex flex-col gap-s2 p-s2`}>
        <div className="flex flex-wrap items-baseline gap-x-s2">
          <div className="min-w-0 flex-1">
            <h3 className="text-sm font-semibold">Preset</h3>
            <p className="text-xs text-ink-3">A starting point. Picking one replaces the preset's rules and keeps your own.</p>
          </div>
          <span className="text-sm">
            <span className="font-medium" data-testid="preset-name">{presetLabel(policy.preset)}</span>
            {customized && <span className="text-ink-3"> · customized</span>}
          </span>
        </div>
        <div className="grid gap-s2 sm:grid-cols-2 xl:grid-cols-4" data-testid="presets">
          {policy.presets.map((p) => (
            <div key={p.name} className={`flex flex-col gap-1 rounded-md border p-s2 ${p.current ? "border-pass/55 bg-pass-soft" : "border-line bg-canvas-2"}`} data-preset={p.name}>
              <h4 className="flex items-center gap-1 text-sm font-medium">
                {presetLabel(p.name)}
                {p.name === "autonomous" && <span className="rounded-sm bg-pass-soft px-1 text-[10px] text-pass">RECOMMENDED</span>}
              </h4>
              <p className="flex-1 text-xs text-ink-2">{p.description}</p>
              <button type="button" className={`${button} mt-1 self-start`} disabled={busy || p.current} onClick={() => usePreset(p.name)}>
                {p.current ? "Current" : `Use ${presetLabel(p.name)}`}
              </button>
            </div>
          ))}
        </div>
        <p className="border-l-2 border-branch px-s1 py-1 text-sm" aria-live="polite" data-testid="ask-rate">
          {!report
            ? "The ask rate is not available right now."
            : report.decisions === 0
              ? `No requests in the last ${report.days} days.`
              : report.asks === 0
                ? `No asks in the last ${report.days} days. Your agents ran uninterrupted.`
                : `${report.asks} ask${report.asks === 1 ? "" : "s"} in the last ${report.days} days (${Math.round(report.ask_rate * 100)}% of ${report.decisions} requests)${report.causes[0] ? `; most from ${report.causes[0].reason}` : ""}.`}
        </p>
      </div>

      <div>
        <h3 className="text-sm font-semibold">Capabilities</h3>
        <p className="text-xs text-ink-3">Set a baseline for each group, then open it to override finer items. Combined commands (a &amp;&amp; b | c) are checked part by part.</p>
      </div>
      <div className={`${card} divide-y divide-line`} data-testid="groups">
        {GROUP_LIST.map((g) => {
          const base = baselineOf(g.id);
          const overrides = g.subs.filter(([s]) => overrideOf(g.id, s) !== "inherit");
          const expanded = open === g.id;
          return (
            <div key={g.id} data-group={g.id}>
              <div className="flex flex-wrap items-center gap-s2 p-s2">
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium">
                    {g.name} <span className="text-[11px] font-normal text-ink-3">{g.subs.length} items</span>
                  </div>
                  <div className="text-xs text-ink-3">
                    {BY_DEFAULT[base] ?? base} · {overrides.length ? `${overrides.length} override${overrides.length === 1 ? "" : "s"}` : "no overrides"}
                  </div>
                </div>
                <Segmented label={`${g.name} baseline`} value={base} levels={BASELINE_LEVELS} disabled={busy} onPick={(l) => setBaseline(g, l)} />
                <button type="button" className={iconButton} aria-expanded={expanded} aria-label={`Show ${g.name} items`} onClick={() => setOpen(expanded ? null : g.id)}>
                  {expanded ? "▾" : "▸"}
                </button>
              </div>
              {expanded && (
                <div className="flex flex-col bg-canvas-2 px-s2 pb-s1" data-testid={`group-${g.id}`}>
                  {g.subs.map(([sub, name, hint]) => {
                    const o = overrideOf(g.id, sub);
                    return (
                      <div key={sub} data-sub={sub} className={`flex flex-wrap items-center gap-s2 border-b border-line border-l-2 py-s1 pl-s1 text-sm last:border-b-0 ${o === "inherit" ? "border-l-transparent" : "border-l-pass bg-pass-soft"}`}>
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-1">
                            {name}
                            {o === "inherit" ? (
                              <span className="text-[11px] text-ink-3">Inherits {LEVEL_NAME[base]}</span>
                            ) : (
                              <span className="text-[11px] text-pass" data-testid="override-marker">
                                {o === base ? `Pinned to ${LEVEL_NAME[o]} (same as group)` : `Overrides ${g.name}: ${LEVEL_NAME[base]} → ${LEVEL_NAME[o]}`}
                              </span>
                            )}
                          </div>
                          <small className="block text-xs text-ink-3">{hint}</small>
                        </div>
                        <Segmented label={`${name} level`} value={o} levels={["inherit", ...BASELINE_LEVELS]} disabled={busy} onPick={(l) => setOverride(g, sub, name, l)} />
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className={`${card} flex flex-wrap items-center justify-between gap-s2 p-s2`}>
        <div>
          <strong className="text-sm font-medium" data-testid="saved-count">
            {policy.saved.length} saved action{policy.saved.length === 1 ? "" : "s"}
          </strong>
          <p className="text-xs text-ink-3">Allows and denials you saved from the Inbox.</p>
        </div>
        <button type="button" className={`${quiet}`} onClick={() => onOpen("saved")}>
          Manage saved actions
        </button>
      </div>
      <div className={`${card} flex flex-wrap items-center justify-between gap-s2 p-s2`}>
        <div>
          <strong className="text-sm font-medium">Advanced</strong>
          <p className="text-xs text-ink-3">{policy.rules.length} rules in total. Search, filter and inspect every rule.</p>
        </div>
        <span className="flex gap-s1">
          <button type="button" className={`${quiet}`} onClick={() => onOpen("activity")}>
            Activity
          </button>
          <button type="button" className={button} onClick={() => onOpen("advanced")}>
            Open advanced view
          </button>
        </span>
      </div>
    </div>
  );
}

/* The flat rule list: search and filter, add, edit and remove, and what a request no rule covers does. */
function AdvancedView({ client, policy, busy, run, record, onEdit, onRemove }: {
  client: DaemonClient;
  policy: PermissionPolicyDTO;
  busy: boolean;
  run(change: () => Promise<unknown>, done: Feedback): Promise<void>;
  record(kind: string, what: string, detail: string): void;
  onEdit(rule: PermissionRuleDTO | "new"): void;
  onRemove(rule: PermissionRuleDTO): void;
}) {
  const [query, setQuery] = useState("");
  const [capability, setCapability] = useState("");
  const [decision, setDecision] = useState("");
  const [source, setSource] = useState("");
  const q = query.trim().toLocaleLowerCase();
  const filtered = !!(q || capability || decision || source);
  const shown = policy.rules.filter(
    (r) =>
      (!capability || ruleGroup(r) === capability) &&
      (!decision || r.decision === decision) &&
      (!source || r.source === source) &&
      [ruleAction(r), r.kind, r.label ?? "", r.describe, r.match, ruleSummary(r), ruleLimits(r), r.source].join(" ").toLocaleLowerCase().includes(q),
  );

  const setUnmatched = (value: string) =>
    void run(async () => {
      await client.setPermissionUnmatched(value as PermissionUnmatched);
      record("Policy", "Unmatched actions changed", UNMATCHED.find((u) => u.value === value)?.label ?? value);
    }, { text: "Unmatched actions updated." });

  return (
    <div className="flex flex-col gap-s2">
      <div className={`${card} flex flex-col gap-s1 p-s2`}>
        <div className="flex flex-wrap items-center gap-s2">
          <div className="min-w-0 flex-1">
            <h3 className="text-sm font-semibold">Rules</h3>
            <p className="text-xs text-ink-3">Each rule says whether an action runs automatically, asks you, or is blocked, and how far an approval may reach. Conflicting rules are checked when you save.</p>
          </div>
          <button type="button" className={button} disabled={busy} onClick={() => onEdit("new")}>
            + Add rule
          </button>
        </div>
        <div className="flex flex-wrap gap-s1">
          <input type="search" className={`${field} min-w-48 flex-1`} aria-label="Search rules" placeholder="Search rules by action, target or decision" value={query} onChange={(e) => setQuery(e.target.value)} />
          <select className={field} aria-label="Capability" value={capability} onChange={(e) => setCapability(e.target.value)}>
            <option value="">Any capability</option>
            {GROUP_LIST.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
            <option value="other">Other</option>
          </select>
          <select className={field} aria-label="Decision filter" value={decision} onChange={(e) => setDecision(e.target.value)}>
            <option value="">Any decision</option>
            {BASELINE_LEVELS.map((l) => (
              <option key={l} value={l}>
                {LEVEL_NAME[l]}
              </option>
            ))}
          </select>
          <select className={field} aria-label="Source" value={source} onChange={(e) => setSource(e.target.value)}>
            <option value="">Any source</option>
            <option value="preset">Preset</option>
            <option value="user">You</option>
            <option value="learned">Learned</option>
          </select>
          <button type="button" className={`${quiet}`} disabled={!filtered} onClick={() => { setQuery(""); setCapability(""); setDecision(""); setSource(""); }}>
            Clear
          </button>
        </div>
        <span className="text-xs text-ink-3" aria-live="polite">
          {filtered ? `${shown.length} of ${policy.rules.length} rules` : `${policy.rules.length} rules`}
        </span>
        <table className="w-full border-collapse text-left text-[13px]" data-testid="rules">
          <thead>
            <tr className="text-[11px] uppercase tracking-wider text-ink-3">
              <th className="border-b border-line py-1 pr-s1 font-medium">Action</th>
              <th className="border-b border-line py-1 pr-s1 font-medium">Policy</th>
              <th className="w-20 border-b border-line py-1" />
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <tr key={r.id} data-rule={r.id} className={r.expired ? "opacity-60" : ""}>
                <td className="border-b border-line py-s1 pr-s1 align-middle">
                  <span className="font-medium">{ruleAction(r)}</span>
                  {r.source !== "user" && <span className="ml-1 rounded-sm border border-line px-1 text-[11px] text-ink-3">{r.source}</span>}
                  <small className={`mt-0.5 block break-all text-xs text-ink-3 ${mono}`}>{r.label || r.describe}</small>
                </td>
                <td className="border-b border-line py-s1 pr-s1 align-middle">
                  <span className={`${pill} ${INK[r.decision] ?? ""}`}>{ruleSummary(r)}</span>
                  <small className="mt-0.5 block text-xs text-ink-3">{ruleLimits(r)}</small>
                  {(r.exclusions ?? []).length > 0 && <small className="mt-0.5 block break-all text-xs text-ink-3">Except: {r.exclusions!.map((x) => x.describe).join("; ")}</small>}
                </td>
                <td className="whitespace-nowrap border-b border-line py-s1 text-right align-middle">
                  <button type="button" className={iconButton} aria-label={`Edit ${ruleAction(r)} rule`} title="Edit rule" disabled={busy} onClick={() => onEdit(r)}>
                    <Pencil />
                  </button>
                  <button type="button" className={`${iconButton} hover:text-fail`} aria-label={`Remove ${ruleAction(r)} rule`} title="Remove rule" disabled={busy} onClick={() => onRemove(r)}>
                    <Bin />
                  </button>
                </td>
              </tr>
            ))}
            {shown.length === 0 && (
              <tr>
                <td colSpan={3} className="p-s2 text-sm text-ink-3">
                  {policy.rules.length ? "No matching rules. Clear the search or try another action or target." : "No rules: every request follows the unmatched setting below."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <div className="flex flex-wrap items-center justify-between gap-s2 pt-s1">
          <div>
            <label htmlFor="permission-unmatched" className="text-sm font-medium">
              Unmatched actions
            </label>
            <p className="text-xs text-ink-3">How to handle a request no rule covers.</p>
          </div>
          <select id="permission-unmatched" className={field} value={policy.unmatched} disabled={busy} onChange={(e) => setUnmatched(e.target.value)}>
            {UNMATCHED.map((u) => (
              <option key={u.value} value={u.value}>
                {u.label}
              </option>
            ))}
          </select>
        </div>
      </div>
    </div>
  );
}

/* One step of an editor sheet. */
function Step({ title, blurb, children }: { title: string; blurb: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-s1 border-b border-line pb-s2 last:border-b-0">
      <h3 className="text-sm font-semibold">{title}</h3>
      <p className="text-xs text-ink-3">{blurb}</p>
      <div className="grid gap-s2 sm:grid-cols-2">{children}</div>
    </section>
  );
}

function Duration({ amount, unit, onAmount, onUnit, name }: { amount: string; unit: Unit; onAmount(v: string): void; onUnit(u: Unit): void; name: string }) {
  return (
    <span className="flex items-center gap-1">
      <input type="number" min={1} className={`${field} w-20`} aria-label={name} value={amount} onChange={(e) => onAmount(e.target.value)} />
      <select className={field} aria-label={`${name} unit`} value={unit} onChange={(e) => onUnit(e.target.value as Unit)}>
        {UNITS.map((u) => (
          <option key={u.value} value={u.value}>
            {u.label}
          </option>
        ))}
      </select>
    </span>
  );
}

const decisionLabel = (d: string) => DECISIONS.find((x) => x.value === d)?.label ?? d;

function RuleCard({ rule, tag }: { rule: RuleShape & { label?: string; describe?: string }; tag: string }) {
  return (
    <article className="border-t border-line py-s2 text-xs">
      <span className="text-[11px] font-medium uppercase tracking-wider text-ink-3">{tag}</span>
      <h4 className="text-sm font-medium">
        {ruleAction(rule)} · {decisionLabel(rule.decision)}
      </h4>
      <p className={`mt-1 break-all text-ink-2 ${mono}`}>{rule.label || rule.describe || `${rule.match_type} ${rule.match}`}</p>
    </article>
  );
}

/* A rule's editor: a sheet in three steps, its live summary at the foot, and the conflict check before a save. */
function RuleEditor({ client, rule, rules, onClose, onSaved }: {
  client: DaemonClient;
  rule: PermissionRuleDTO | null;
  rules: PermissionRuleDTO[];
  onClose(): void;
  onSaved(what: string, detail: string): void;
}) {
  const push = rule?.match_type === "push";
  const [action, setAction] = useState(rule ? (push ? "push" : rule.kind) : "bash");
  const [matchType, setMatchType] = useState(rule?.match_type ?? "exact");
  const [match, setMatch] = useState(rule?.match ?? "");
  const [name, setName] = useState(rule?.label ?? "");
  const [decision, setDecision] = useState(rule?.decision ?? "ask");
  const limits = rule?.limits;
  const [maxScope, setMaxScope] = useState(limits?.max_scope ?? (push ? "destination" : ""));
  // "once": no saved approval settles the ask (the release-branch push rules).
  const [life, setLife] = useState<PermissionLife | "once">((limits?.life as PermissionLife | "once") ?? "session");
  const cap = splitMinutes(limits?.minutes ?? 30);
  const [amount, setAmount] = useState(String(cap.value));
  const [unit, setUnit] = useState<Unit>(cap.unit);
  const [timed, setTimed] = useState(!!rule?.expires && !rule.expired);
  const [ruleAmount, setRuleAmount] = useState("1");
  const [ruleUnit, setRuleUnit] = useState<Unit>("hours");
  const [conflicts, setConflicts] = useState<Conflict[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const named = !PLAIN_MATCHES.includes(matchType) && matchType !== "push";
  const draft: RuleShape & { label: string } = { id: rule?.id, kind: action === "push" ? "bash" : action, match: match.trim(), match_type: matchType, decision, label: name.trim() };
  const minutes = toMinutes(Number(amount), unit);
  const ruleMinutes = toMinutes(Number(ruleAmount), ruleUnit);
  const askLimits = { life, ...(life === "time" ? { minutes } : {}), ...(maxScope.trim() ? { max_scope: maxScope.trim() } : {}) };

  const pickAction = (a: string) => {
    setAction(a);
    if (a === "push") {
      setMatchType("push");
      if (!maxScope) setMaxScope("destination");
    } else if (matchType === "push" || named) {
      setMatchType("exact");
      setMatch("");
    }
    if (a !== "push" && LEVELS.includes(maxScope as never)) setMaxScope("");
  };

  const invalid = (): string | null => {
    if (!draft.match) return "Choose a target: the files, command, URL or tool this rule covers.";
    if (matchType === "push" && !draft.match.includes(" → ")) return "A push rule's target is “source → destination”, for example * → @default.";
    if (decision === "ask" && life === "time" && !(minutes > 0)) return "Choose a positive duration.";
    if (decision === "allow" && timed && !(ruleMinutes > 0)) return "Choose a positive duration.";
    return null;
  };

  const body = (): PermissionRulePut & { kind: PermissionKind; match: string; decision: PermissionDecision } => ({
    kind: draft.kind as PermissionKind,
    match: draft.match,
    match_type: matchType as PermissionMatchType,
    decision: decision as PermissionDecision,
    label: draft.label,
    limits: decision === "ask" ? askLimits : null,
    ...(decision === "allow" ? (timed ? { minutes: ruleMinutes } : { expires: null }) : {}),
  });
  const persist = async () => (rule ? client.updatePermissionRule(rule.id, body()) : client.createPermissionRule(body()));
  const detail = `${ruleAction(draft)} · ${draft.label || draft.match} · ${decisionLabel(decision)}`;

  const attempt = async (work: () => Promise<void>) => {
    setSaving(true);
    setError(null);
    try {
      await work();
    } catch (e) {
      setError(message(e));
    } finally {
      setSaving(false);
    }
  };
  const save = () => {
    const why = invalid();
    if (why) return setError(why);
    const found = conflictsFor(draft, rules);
    if (found.length) return setConflicts(found);
    void attempt(async () => {
      await persist();
      onSaved(rule ? "Rule saved" : "Rule added", detail);
    });
  };
  // Replace: the conflicting rules go, then this one is saved.
  const useMine = () =>
    void attempt(async () => {
      for (const c of conflicts ?? []) await client.deletePermissionRule(c.rule.id);
      await persist();
      onSaved("Conflict resolved: your rule replaced the conflicting ones", detail);
    });
  // Auto-resolve: the stricter decision keeps the shared requests, the other rule gets them as an exception.
  const plainRule = (r: RuleShape) => PLAIN_MATCHES.includes(r.match_type);
  const autoOk = (conflicts ?? []).every((c) => c.certain && (strength(decision) > strength(c.rule.decision) ? plainRule(draft) : plainRule(c.rule)));
  const autoResolve = () =>
    void attempt(async () => {
      const saved = await persist();
      for (const c of conflicts ?? []) {
        const [into, except] = strength(decision) > strength(c.rule.decision) ? [c.rule.id, draft] : [saved.id, c.rule];
        await client.addPermissionException(into, { kind: except.kind as PermissionKind, match: except.match, match_type: except.match_type as "exact" });
      }
      onSaved("Conflict resolved: the stricter decision wins where the rules meet", detail);
    });

  const strongest = [draft, ...(conflicts ?? []).map((c) => c.rule)].sort((a, b) => strength(b.decision) - strength(a.decision))[0];
  const summary = `${ruleAction(draft)} · ${draft.label || draft.match || "no target yet"} · ${decisionLabel(decision)}${decision === "ask" ? ` · ${limitsText(askLimits)}` : decision === "allow" && timed ? ` · for ${ruleAmount} ${ruleUnit}` : ""}`;
  const placeholder = action === "push" ? "* → @default" : action === "bash" ? "npm test" : action === "fetch" ? "https://docs.example.com/*" : action === "mcp" ? "mcp__github__create_issue" : "docs/**";

  return (
    <Sheet
      fixed
      label="Rule editor"
      testId="rule-editor"
      title={conflicts ? "Resolve conflicting rules" : rule ? "Edit rule" : "New rule"}
      onClose={onClose}
      footer={
        conflicts ? undefined : (
          <>
            <p className="min-w-0 flex-1 text-xs text-ink-2" aria-live="polite" data-testid="rule-summary">
              {summary}
            </p>
            <button type="button" className={quiet} onClick={onClose}>
              Cancel
            </button>
            <button type="button" className={button} disabled={saving} onClick={save}>
              {saving ? "Saving…" : "Save rule"}
            </button>
          </>
        )
      }
    >
      {conflicts ? (
        <div className="flex flex-col gap-s2" data-testid="conflict-review">
          <div className="border-l-2 border-sap px-s2 py-s1 text-sm">
            <span className="text-[11px] font-medium uppercase tracking-wider text-ink-3">
              {conflicts.length} {conflicts.every((c) => c.certain) ? `conflict${conflicts.length === 1 ? "" : "s"}` : "possible overlap"} found
            </span>
            <h3 className="font-medium">Different decisions for the same requests</h3>
            <p className="mt-1 text-ink-2">
              These rules cover some of the same requests as yours and disagree on whether {ruleAction(draft).toLowerCase()} is allowed, asks, or is blocked.
            </p>
            <p className="mt-1 font-medium">Until resolved: {decisionLabel(strongest.decision)} wins on the shared requests.</p>
            {!autoOk && <p className="mt-1 text-xs text-ink-3">Some of these patterns can't be proven to overlap or to be apart, so they can't be resolved automatically.</p>}
          </div>
          <div className="grid gap-s1 sm:grid-cols-2">
            <RuleCard rule={draft} tag="Your unsaved rule" />
            {conflicts.map((c) => (
              <RuleCard key={c.rule.id} rule={c.rule} tag={`Existing rule · ${c.rule.source}`} />
            ))}
          </div>
          <div className="flex flex-wrap gap-s1">
            <button type="button" className={button} disabled={saving} onClick={onClose}>
              Keep existing rules
            </button>
            <button type="button" className={button} disabled={saving} onClick={useMine}>
              Use my new rule
            </button>
            <button type="button" className={button} disabled={saving || !autoOk} onClick={autoResolve}>
              Auto-resolve
            </button>
          </div>
          <p className="text-xs text-ink-3">Keep existing discards your changes. Use my new rule removes the conflicting rules. Auto-resolve keeps the stricter decision on the shared requests by adding an exception to the other rule.</p>
          <button type="button" className={`${quiet} self-start`} onClick={() => setConflicts(null)}>
            Back to editing
          </button>
        </div>
      ) : (
        <>
          <Step title="1. Action" blurb="The kind of request this rule covers, and which ones.">
            <label className={fieldLabel}>
              Action
              <select className={field} aria-label="Action" value={action} onChange={(e) => pickAction(e.target.value)}>
                {ACTIONS.map((a) => (
                  <option key={a} value={a}>
                    {kindLabel(a)}
                  </option>
                ))}
              </select>
            </label>
            {action !== "push" && (
              <label className={fieldLabel}>
                Match
                <select className={field} aria-label="Match" value={matchType} onChange={(e) => setMatchType(e.target.value)}>
                  {named && <option value={matchType}>Built-in: {matchType}</option>}
                  {PLAIN_MATCHES.map((m) => (
                    <option key={m} value={m}>
                      {MATCH_LABEL[m]}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label className={`${fieldLabel} sm:col-span-2`}>
              Target
              <input className={`${field} font-mono`} aria-label="Target" value={match} readOnly={named} placeholder={placeholder} onChange={(e) => setMatch(e.target.value)} />
              <span className="font-normal">
                {named
                  ? "A built-in matcher: brindle decides what it covers. Pick another match to write your own target."
                  : action === "push"
                    ? "source → destination; * for any branch, @default for the repo's default branch."
                    : matchType === "glob"
                      ? "* matches anything; docs/** matches files in docs and its folders."
                      : ""}
              </span>
            </label>
            <label className={`${fieldLabel} sm:col-span-2`}>
              Name (optional)
              <input className={field} aria-label="Rule name" value={name} placeholder="Shown in the rules list" onChange={(e) => setName(e.target.value)} />
            </label>
          </Step>
          <Step title="2. Access" blurb="Whether the action runs automatically, asks you, or is blocked.">
            <label className={fieldLabel}>
              Decision
              <select className={field} aria-label="Decision" value={decision} onChange={(e) => setDecision(e.target.value)}>
                {DECISIONS.map((d) => (
                  <option key={d.value} value={d.value}>
                    {d.label}
                  </option>
                ))}
              </select>
            </label>
          </Step>
          {decision === "ask" && (
            <Step title="3. Approval limits" blurb="How far an approval you give from the Inbox may reach, and how long it may be remembered.">
              {action === "push" ? (
                <label className={fieldLabel}>
                  Maximum approval scope
                  <select className={field} aria-label="Maximum approval scope" value={maxScope} onChange={(e) => setMaxScope(e.target.value)}>
                    {LEVELS.map((l) => (
                      <option key={l} value={l}>
                        {LEVEL_LABEL[l]}
                      </option>
                    ))}
                  </select>
                </label>
              ) : (
                <label className={fieldLabel}>
                  Approvals stay within
                  <input className={`${field} font-mono`} aria-label="Approvals stay within" value={maxScope} placeholder="Anything (leave empty)" onChange={(e) => setMaxScope(e.target.value)} />
                </label>
              )}
              <label className={fieldLabel}>
                Remember approvals for
                <select className={field} aria-label="Remember approvals for" value={life} onChange={(e) => setLife(e.target.value as PermissionLife | "once")}>
                  <option value="once">Never: ask every time</option>
                  <option value="session">This session</option>
                  <option value="time">Up to a duration</option>
                  <option value="forever">Until revoked</option>
                </select>
              </label>
              {life === "time" && (
                <div className={fieldLabel}>
                  Longest approval
                  <Duration amount={amount} unit={unit} onAmount={setAmount} onUnit={setUnit} name="Longest approval" />
                </div>
              )}
              <p className="text-xs text-ink-3 sm:col-span-2">
                {life === "once" ? "Each request asks: an approval is never saved." : life === "session" ? "An approval ends with the session it was given in." : life === "forever" ? "Approvals can be saved until you delete them in Saved actions." : "You may approve once or for up to this long. Time starts when you approve."}
              </p>
            </Step>
          )}
          {decision === "allow" && (
            <Step title="3. Rule lifetime" blurb="How long this automatic rule stays active.">
              <label className={fieldLabel}>
                Active
                <select className={field} aria-label="Rule lifetime" value={timed ? "time" : "forever"} onChange={(e) => setTimed(e.target.value === "time")}>
                  <option value="forever">Until removed</option>
                  <option value="time">For a duration</option>
                </select>
              </label>
              {timed && (
                <div className={fieldLabel}>
                  Duration
                  <Duration amount={ruleAmount} unit={ruleUnit} onAmount={setRuleAmount} onUnit={setRuleUnit} name="Rule duration" />
                </div>
              )}
              {rule?.expires && <p className="text-xs text-ink-3 sm:col-span-2">{rule.expired ? "Expired" : "Now active until"} {when(rule.expires)}. Saving restarts it.</p>}
            </Step>
          )}
          {error && (
            <p role="alert" className={alert}>
              {error}
            </p>
          )}
        </>
      )}
      {conflicts && error && (
        <p role="alert" className={alert}>
          {error}
        </p>
      )}
    </Sheet>
  );
}

function SavedTab({ saved, busy, onEdit, onDelete }: { saved: SavedActionDTO[]; busy: boolean; onEdit(s: SavedActionDTO): void; onDelete(s: SavedActionDTO): void }) {
  return (
    <div className={`${card} flex flex-col gap-s1 p-s2`}>
      <h3 className="text-sm font-semibold">Your saved actions</h3>
      <p className="text-xs text-ink-3">Allows and denials you saved from the Inbox. Open one to change its scope, decision or how long it lasts, or delete it. Your rules are still checked on every use.</p>
      <table className="w-full border-collapse text-left text-[13px]" data-testid="saved">
        <thead>
          <tr className="text-[11px] uppercase tracking-wider text-ink-3">
            <th className="border-b border-line py-1 pr-s1 font-medium">Action & scope</th>
            <th className="border-b border-line py-1 pr-s1 font-medium">Decision</th>
            <th className="border-b border-line py-1 pr-s1 font-medium">Lasts</th>
            <th className="w-20 border-b border-line py-1" />
          </tr>
        </thead>
        <tbody>
          {saved.map((s) => (
            <tr key={s.id} data-saved={s.id} className={s.expired ? "opacity-60" : ""}>
              <td className="border-b border-line py-s1 pr-s1">
                <span className="font-medium">{kindLabel(s.kind)}</span>
                <small className={`mt-0.5 block break-all text-xs text-ink-3 ${mono}`}>
                  {s.pattern}
                  {s.note ? ` · ${s.note}` : ""}
                </small>
              </td>
              <td className="border-b border-line py-s1 pr-s1">
                <span className={`${pill} ${s.decision === "deny" ? INK.deny : INK.allow}`}>{s.decision === "deny" ? "Denied" : "Allowed"}</span>
              </td>
              <td className="border-b border-line py-s1 pr-s1 text-xs">{savedLasts(s)}</td>
              <td className="whitespace-nowrap border-b border-line py-s1 text-right">
                <button type="button" className={iconButton} aria-label={`Edit ${kindLabel(s.kind)} saved action`} title="Edit" disabled={busy} onClick={() => onEdit(s)}>
                  <Pencil />
                </button>
                <button type="button" className={`${iconButton} hover:text-fail`} aria-label={`Delete ${kindLabel(s.kind)} saved action`} title="Delete" disabled={busy} onClick={() => onDelete(s)}>
                  <Bin />
                </button>
              </td>
            </tr>
          ))}
          {saved.length === 0 && (
            <tr>
              <td colSpan={4} className="p-s2 text-sm text-ink-3">
                No saved actions. Allow or deny something in the Inbox and choose to remember it.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

const SPECIFIC_KINDS = ["read", "write", "edit", "bash", "fetch"];

/* A saved action's editor: the same sheet as a rule's, in three steps (what it covers, its decision, how long it lasts). */
function SavedEditor({ client, saved: s, onClose, onDelete, onSaved }: { client: DaemonClient; saved: SavedActionDTO; onClose(): void; onDelete(): void; onSaved(s: SavedActionDTO): void }) {
  const scopes = useMemo(() => savedScopes(s), [s]);
  const [scope, setScope] = useState(scopes.find((r) => r.pattern === s.pattern)?.value ?? scopes[0].value);
  const [specific, setSpecific] = useState(s.pattern);
  const [decision, setDecision] = useState(s.decision);
  const [life, setLife] = useState<PermissionLife>((s.life as PermissionLife) ?? "forever");
  const lasted = splitMinutes(s.minutes ?? 30);
  const [amount, setAmount] = useState(String(lasted.value));
  const [unit, setUnit] = useState<Unit>(lasted.unit);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const minutes = toMinutes(Number(amount), unit);
  const choice = scope === "specific" ? specificTarget(s, specific) : scopes.find((r) => r.value === scope);

  const save = async () => {
    if (!choice || typeof choice === "string") return setError(choice ?? "Choose a scope.");
    if (life === "time" && !(minutes > 0)) return setError("Choose a positive duration.");
    const pick: AskScopeDTO = choice;
    const fromRepo = typeof s.request?.repo_root === "string" && s.request.repo_root ? s.request.repo_root : null;
    const repo = pick.value === "all" ? null : (s.repo ?? fromRepo);
    setSaving(true);
    setError(null);
    try {
      const got = await client.updateSavedAction(s.id, {
        pattern: pick.pattern,
        scope: pick.value,
        level: (pick.level as SavedActionPut["level"]) ?? null,
        decision: decision as "allow" | "deny",
        life,
        minutes: life === "time" ? minutes : null,
        note: pick.label,
        ...(repo !== (s.repo ?? null) ? { repo } : {}),
      });
      onSaved(got);
    } catch (e) {
      setError(message(e));
    } finally {
      setSaving(false);
    }
  };
  const covers = scope === "specific" ? specific.trim() || "a specific target" : (scopes.find((r) => r.value === scope)?.label ?? "");

  return (
    <Sheet
      fixed
      label="Saved action editor"
      testId="saved-editor"
      title="Edit saved action"
      onClose={onClose}
      footer={
        <>
          <button type="button" className={`${quiet} mr-auto text-fail`} disabled={saving} onClick={onDelete}>
            Delete
          </button>
          <button type="button" className={quiet} onClick={onClose}>
            Cancel
          </button>
          <button type="button" className={button} disabled={saving} onClick={() => void save()}>
            {saving ? "Saving…" : "Save changes"}
          </button>
        </>
      }
    >
      <Step title="1. What it covers" blurb="The action and scope this saved decision applies to. Narrow the scope to be asked again outside it.">
        <label className={fieldLabel}>
          Action
          <input className={`${field} bg-canvas-2`} readOnly value={kindLabel(s.kind)} />
        </label>
        <label className={`${fieldLabel} sm:col-span-2`}>
          Scope
          <select className={field} aria-label="Scope" value={scope} onChange={(e) => setScope(e.target.value)}>
            {scopes.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
            {SPECIFIC_KINDS.includes(s.kind) && <option value="specific">A specific target inside the current scope…</option>}
          </select>
        </label>
        {scope === "specific" && (
          <label className={`${fieldLabel} sm:col-span-2`}>
            Specific target
            <input className={`${field} font-mono`} aria-label="Specific target" value={specific} onChange={(e) => setSpecific(e.target.value)} />
          </label>
        )}
        <p className="text-xs text-ink-3 sm:col-span-2">Your rules still limit where it applies.</p>
      </Step>
      <Step title="2. Decision" blurb="What happens the next time a request matches, without asking you.">
        <label className={fieldLabel}>
          Decision
          <select className={field} aria-label="Decision" value={decision} onChange={(e) => setDecision(e.target.value)}>
            <option value="allow">Allow without asking</option>
            <option value="deny">Deny without asking</option>
          </select>
        </label>
      </Step>
      <Step title="3. How long it lasts" blurb="Your rules are still checked on every use.">
        <label className={fieldLabel}>
          Lasts
          <select className={field} aria-label="Lasts" value={life} onChange={(e) => setLife(e.target.value as PermissionLife)}>
            {s.session && <option value="session">This session</option>}
            <option value="time">Custom duration</option>
            <option value="forever">Until deleted</option>
          </select>
        </label>
        {life === "time" && (
          <div className={fieldLabel}>
            Duration
            <Duration amount={amount} unit={unit} onAmount={setAmount} onUnit={setUnit} name="Saved action duration" />
          </div>
        )}
        <p className="text-xs text-ink-3 sm:col-span-2">
          {life === "session" ? "Ends when the session it was saved in closes." : life === "forever" ? "Stays until you delete it here." : "Expires after this time, counted from when you save."}
        </p>
      </Step>
      <p className={`border-l-2 px-s1 py-1 text-xs text-ink-2 ${decision === "deny" ? "border-sap" : "border-pass"}`} aria-live="polite" data-testid="saved-summary">
        {decision === "deny" ? "Deny" : "Allow"} without asking · {kindLabel(s.kind)} · {covers} · {life === "session" ? "this session" : life === "forever" ? "until deleted" : `${amount} ${unit}`}
      </p>
      {error && (
        <p role="alert" className={alert}>
          {error}
        </p>
      )}
    </Sheet>
  );
}

function ActivityTab({ policy, log }: { policy: PermissionPolicyDTO; log: Activity[] }) {
  const entries = [
    ...log,
    ...policy.rules
      .filter((r) => (r.source === "user" || r.source === "learned") && (r.created ?? 0) > 0)
      .map((r) => ({ kind: "Policy", what: r.source === "learned" ? "Rule learned from your approvals" : "Rule added", detail: `${ruleAction(r)} · ${r.label || r.describe} · ${ruleSummary(r)}`, at: r.created ?? 0 })),
    ...policy.saved
      .filter((s) => (s.created ?? 0) > 0)
      .map((s) => ({ kind: s.decision === "deny" ? "Deny" : "Approval", what: s.decision === "deny" ? "Denial saved" : "Approval saved", detail: `${kindLabel(s.kind)} · ${s.pattern} · ${savedLasts(s)}`, at: s.created ?? 0 })),
  ].sort((a, b) => b.at - a.at);
  return (
    <div className={`${card} flex flex-col p-s2`} data-testid="activity">
      <h3 className="text-sm font-semibold">Permission activity</h3>
      <p className="text-xs text-ink-3">Rule changes, saved approvals and denials, newest first.</p>
      {entries.length === 0 && <p className="py-s2 text-sm text-ink-3">Nothing yet.</p>}
      {entries.map((a, i) => (
        <div key={`${a.at}:${i}`} className="flex gap-s2 border-b border-line py-s1 last:border-b-0">
          <span className="w-16 shrink-0 text-xs text-ink-3">{a.kind}</span>
          <div className="min-w-0">
            <strong className="block text-sm font-medium">{a.what}</strong>
            <p className="break-all text-xs text-ink-3">
              {a.detail} · {when(a.at)}
            </p>
          </div>
        </div>
      ))}
    </div>
  );
}
