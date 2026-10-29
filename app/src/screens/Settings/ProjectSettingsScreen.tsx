/* The repo view of Settings (the switch's second side): this repo's own
   settings next to the person's. Every setting row says in one plain line
   where its value comes from (statusLine): the team's committed
   .brindle/config.json (editing it changes that file, with a warning),
   just for you (the gitignored .brindle/config.local.json, with Clear), or
   the same as yours. A team-set row also offers "Change just for me", which
   writes the value to the local file. The repo-only sections (checks, the
   rules only a repo's files carry) save to the local file too
   (PATCH /v1/config, scope repo). The page asks GET /v1/config without the
   stored keys (keys=0): they are the slow part of that route and nothing
   here shows them. Each section renders at once and fills in when the
   config arrives, so the page never waits on it. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Config, DaemonClient } from "../../api/client";
import { alert, button, notice, panel, quiet } from "../../components/ui";
import { More, message, RestOfConfig, Section, SettingRow } from "./SettingsScreen";
import { PROJECT_RULES, SETTINGS, YOURS, carryEdits, changed, draftOf, invalid, scopeOf, statusLine, values, type Setting } from "./settings";

const only = (...keys: string[]) => PROJECT_RULES.filter((s) => keys.includes(s.key));
const CHECKS = only("checks");
const SETUP = only("setup", "base_branch");
const RULES = only("rules");

const LOADING = "px-s2 py-s1 text-sm text-ink-3";

export function RepoSettings({ client, repo }: { client: DaemonClient; repo: string }) {
  const [config, setConfig] = useState<Config | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const seq = useRef(0);
  const load = useCallback(() => {
    const mine = ++seq.current;
    return client.config(repo, { keys: false }).then(
      (c) => {
        if (mine !== seq.current) return;
        setConfig(c);
        setLoadError(null);
      },
      (e: unknown) => {
        if (mine === seq.current) setLoadError(message(e));
      },
    );
  }, [client, repo]);
  useEffect(() => {
    void load();
  }, [load]);

  /* The repo's settings as a draft; an unsaved edit survives a reload after another change here. */
  const cleared = useRef<string[]>([]);
  const loaded = useMemo(() => draftOf(config?.repo ?? {}), [config]);
  const [base, setBase] = useState(loaded);
  const [draft, setDraft] = useState(loaded);
  if (config && base !== loaded) {
    setBase(loaded);
    // A cleared row takes the value it now inherits, not its old draft; other rows keep their edits.
    setDraft({ ...carryEdits(base, draft, loaded), ...Object.fromEntries(cleared.current.map((k) => [k, loaded[k]])) });
    cleared.current = [];
  }
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [rest, setRest] = useState(false);
  const dirty = config ? changed(loaded, draft) : [];
  const missing = invalid(draft);
  const edit = (key: string, v: unknown) => {
    setSaved(null);
    setDraft((d) => ({ ...d, [key]: v }));
  };
  const sourceOf = (key: string) => scopeOf(config?.overrides, key);
  /* An edit to a row the team sets goes to the committed file; every other edit stays local. */
  const toTeam = dirty.filter((k) => YOURS.some((s) => s.key === k) && sourceOf(k) === "team");

  const run = async (what: () => Promise<void>) => {
    if (saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      await what();
      await load();
    } catch (e) {
      setSaveError(message(e));
    } finally {
      setSaving(false);
    }
  };
  const save = () => {
    if (!config || dirty.length === 0 || missing.length > 0) return;
    const all = values(loaded, draft);
    const team = Object.fromEntries(toTeam.map((k) => [k, all[k]]));
    const mine = Object.fromEntries(Object.entries(all).filter(([k]) => !toTeam.includes(k)));
    void run(async () => {
      const done: string[] = [];
      if (toTeam.length > 0) done.push(...(await client.patchConfig({ scope: "shared", repo, values: team })).updated);
      if (Object.keys(mine).length > 0) done.push(...(await client.patchConfig({ scope: "repo", repo, values: mine })).updated);
      setSaved(`Saved ${done.join(", ")}`);
    });
  };
  const justForMe = (key: string) => run(async () => void (await client.patchConfig({ scope: "repo", repo, values: { [key]: draft[key] } })));
  const clearLocal = (key: string) =>
    run(async () => {
      await client.patchConfig({ scope: "repo", repo, unset: [key] });
      cleared.current = [key];
    });

  const shown = SETTINGS.map((s) => s.key);
  const restLines: [string, string][] = Object.entries(config?.repo ?? {})
    .filter(([k]) => !shown.includes(k))
    .map(([k, v]) => [k, typeof v === "string" ? v : JSON.stringify(v)]);
  const checkRow = (s: Setting) => <SettingRow key={s.key} s={s} value={draft[s.key]} dirty={dirty.includes(s.key)} disabled={saving} onChange={(v) => edit(s.key, v)} />;
  const row = (s: Setting) => {
    const source = sourceOf(s.key);
    const status = (
      <span className="mt-0.5 flex flex-wrap items-center gap-x-s2" data-testid={`status-${s.key}`} data-source={source}>
        <span className="text-ink-2">{statusLine(s, source, config?.user[s.key])}</span>
        {source === "local" && (
          <button type="button" className={`${quiet} text-xs`} disabled={saving} onClick={() => void clearLocal(s.key)}>
            Clear
          </button>
        )}
        {source === "team" && (
          <button type="button" className={`${quiet} text-xs`} disabled={saving} onClick={() => void justForMe(s.key)}>
            Change just for me
          </button>
        )}
      </span>
    );
    return <SettingRow key={s.key} s={s} value={draft[s.key]} dirty={dirty.includes(s.key)} disabled={saving} onChange={(v) => edit(s.key, v)} override={status} />;
  };
  const loading = (testId: string, what: string) => (loadError ? null : <div className={LOADING} data-testid={testId}>{`Loading ${what}…`}</div>);

  return (
    // Room at the foot for the save bar, which sticks there while there are changes.
    <div className={`flex min-h-0 flex-col gap-s3 ${dirty.length > 0 ? "pb-s12" : ""}`}>
      {loadError && (
        <div role="alert" className={`${alert} flex items-center justify-between gap-s2`}>
          <span>Could not load project settings: {loadError}</span>
          <button type="button" className={button} onClick={() => void load()}>
            Retry
          </button>
        </div>
      )}

      <Section id="repo-settings-list" title="Settings" blurb="Each one says whether the team, you alone, or your own settings decide it." testId="repo-setting-rows">
        {config ? YOURS.map(row) : loading("settings-loading", "settings")}
      </Section>

      <Section id="checks" title="Checks" blurb="Run before a branch merges." testId="project-checks">
        {config ? CHECKS.map(checkRow) : loading("checks-loading", "checks")}
      </Section>

      <Section id="setup" title="Setup and PR branch" blurb="How a new worktree starts, and where pull requests go." testId="project-setup">
        {config ? SETUP.map(checkRow) : loading("setup-loading", "setup")}
      </Section>

      <Section id="this-repo" title="Project rules" blurb="Only a repo's own files carry these, so they save to this repo." testId="repo-settings">
        {config ? (
          <>
            {RULES.map(checkRow)}
            <div className="flex flex-col gap-1 px-s2 py-1">
              {restLines.length > 0 ? (
                <>
                  <More open={rest} onToggle={() => setRest((r) => !r)} label="Everything else" count={restLines.length} />
                  {rest && <RestOfConfig lines={restLines} testId="config-rest" note="Edit these in .brindle/config.json (the repo's, committed) or .brindle/config.local.json (yours)." />}
                </>
              ) : (
                <p className="text-xs text-ink-3">Nothing else is set in this repo's files.</p>
              )}
            </div>
          </>
        ) : (
          loading("rules-loading", "rules")
        )}
      </Section>

      {saved && dirty.length === 0 && (
        <p role="status" className={notice}>
          {saved}
        </p>
      )}
      {saveError && dirty.length === 0 && (
        <p role="alert" className={alert}>
          Could not save: {saveError}
        </p>
      )}
      {dirty.length > 0 && (
        <div className={`${panel} sticky bottom-0 flex flex-col gap-s1 border-pass/55 px-s2 py-s2 shadow-lg`} data-testid="settings-save">
          <div className="flex flex-wrap items-center gap-s2">
            <span className="text-sm font-medium">{dirty.length === 1 ? "1 change" : `${dirty.length} changes`}</span>
            <span className="flex-1" />
            <button type="button" className={quiet} disabled={saving} onClick={() => setDraft(loaded)}>
              Discard
            </button>
            <button type="button" className={button} disabled={saving || missing.length > 0} onClick={save}>
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
          <p className="text-xs text-ink-3">
            {toTeam.length > 0
              ? `${toTeam.join(", ")} would change .brindle/config.json: committed, so this changes the file for everyone on the repo. The rest saves to .brindle/config.local.json, not committed.`
              : "Saved to this repo: .brindle/config.local.json, not committed."}
          </p>
          {missing.length > 0 && (
            <p className={alert} data-testid="settings-invalid">
              {missing.map((k) => SETTINGS.find((s) => s.key === k)?.label ?? k).join(", ")} needs a number: the engine keeps what is saved as it is, and an empty value would break it.
            </p>
          )}
          {saveError && (
            <p role="alert" className={alert}>
              Could not save: {saveError}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
