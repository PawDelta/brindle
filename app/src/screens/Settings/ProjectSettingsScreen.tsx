/* Project settings: one repo's own settings, apart from the person's (Settings).
   Its checks and the other rules only its files carry (saved to the repo's
   gitignored .brindle/config.local.json with PATCH /v1/config, scope repo),
   and the settings it overrides of the person's own, each saying what it
   sets and what it overrides, with Clear for the override (the same PATCH
   with `unset`; the committed file's own override has its own explicit
   clear). Plan approvals has its own override here (Governance.tsx, PUT
   /v1/governance/plan). The page asks GET /v1/config without the stored keys (keys=0):
   they are the slow part of that route and nothing here shows them. The
   header and every section render at once; each fills in when the config
   arrives, so the page never waits on it. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Config, DaemonClient } from "../../api/client";
import { PageHeader } from "../../components/PageHeader";
import { NO_REPO, RepoSelect, useRepo } from "../../components/RepoSelect";
import { alert, button, notice, panel, quiet } from "../../components/ui";
import { useSessions } from "../../store/sessions";
import { PLAN_HELP, PlanChoice } from "./Governance";
import { More, message, RestOfConfig, Row, Section, SettingRow } from "./SettingsScreen";
import { HANDLED, PROJECT_RULES, SETTINGS, YOURS, carryEdits, changed, draftOf, invalid, overrideLabel, values } from "./settings";

const CHECKS = PROJECT_RULES.filter((s) => s.key === "checks");
const RULES = PROJECT_RULES.filter((s) => s.key !== "checks");

const LOADING = "px-s2 py-s1 text-sm text-ink-3";

function Project({ client, repo }: { client: DaemonClient; repo: string }) {
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
  const loaded = useMemo(() => draftOf(config?.repo ?? {}), [config]);
  const [base, setBase] = useState(loaded);
  const [draft, setDraft] = useState(loaded);
  if (config && base !== loaded) {
    setBase(loaded);
    setDraft(carryEdits(base, draft, loaded));
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

  const save = async () => {
    if (!config || dirty.length === 0 || saving || missing.length > 0) return;
    setSaving(true);
    setSaveError(null);
    try {
      const ack = await client.patchConfig({ scope: "repo", repo, values: values(loaded, draft) });
      setSaved(`Saved ${ack.updated.join(", ")} to ${ack.path}`);
      await load();
    } catch (e) {
      setSaveError(message(e));
    } finally {
      setSaving(false);
    }
  };
  const clearOverride = async (key: string, source: "local" | "repo") => {
    if (saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      await client.patchConfig({ scope: source === "repo" ? "shared" : "repo", repo, unset: [key] });
      await load();
    } catch (e) {
      setSaveError(message(e));
    } finally {
      setSaving(false);
    }
  };

  const restLines: [string, string][] = Object.entries(config?.repo ?? {})
    .filter(([k]) => !HANDLED.includes(k))
    .map(([k, v]) => [k, typeof v === "string" ? v : JSON.stringify(v)]);
  const overridden = YOURS.filter((s) => config?.overrides?.[s.key]);
  const row = (s: (typeof SETTINGS)[number]) => <SettingRow key={s.key} s={s} value={draft[s.key]} dirty={dirty.includes(s.key)} disabled={saving} onChange={(v) => edit(s.key, v)} />;
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

      <Section id="checks" title="Checks" blurb="Run before a branch merges." testId="project-checks">
        {config ? CHECKS.map(row) : loading("checks-loading", "checks")}
      </Section>

      <Section id="this-repo" title="Project rules" blurb="Only a repo's own files carry these, so they save to this repo." testId="repo-settings">
        {config ? (
          <>
            {RULES.map(row)}
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

      <Section id="project-plans" title="Plan approvals" blurb="This project's own choice, saved to .brindle/config.local.json." testId="project-plan-approvals">
        <Row label="Plan approvals" help={PLAN_HELP} htmlFor="plan-project">
          <PlanChoice client={client} repo={repo} id="plan-project" />
        </Row>
      </Section>

      <Section id="overrides" title="Overrides of your settings" blurb="Settings this project sets instead of yours." testId="project-overrides">
        {!config ? (
          loading("overrides-loading", "overrides")
        ) : overridden.length === 0 ? (
          <p className={`${LOADING}`}>This project overrides none of your settings.</p>
        ) : (
          overridden.map((s) => {
            const source = config.overrides![s.key];
            return (
              <div key={s.key} className="flex flex-wrap items-center gap-x-s2 gap-y-1 px-s2 py-s1" data-testid={`override-${s.key}`} data-source={source}>
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium">{s.label}</div>
                  <div className="text-xs text-ink-2" data-testid={`override-label-${s.key}`}>
                    {overrideLabel(s, config.repo?.[s.key], config.user[s.key])}
                  </div>
                  {source === "repo" && <div className="text-xs text-ink-3">Committed: this changes the file for everyone on the repo.</div>}
                </div>
                <button type="button" className={`${quiet} text-xs`} disabled={saving} onClick={() => void clearOverride(s.key, source)}>
                  {source === "local" ? "Clear override" : "Clear in .brindle/config.json"}
                </button>
              </div>
            );
          })
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
            <button type="button" className={button} disabled={saving || missing.length > 0} onClick={() => void save()}>
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
          <p className="text-xs text-ink-3">Saved to this repo: .brindle/config.local.json, not committed.</p>
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

/* `repo`: the project the route names; without one, the first the daemon knows (a picker when there are several). */
export function ProjectSettingsScreen({ client, repo: routed, onSettings }: { client: DaemonClient; repo?: string; onSettings(): void }) {
  const pick = useRepo();
  const { repos } = useSessions();
  const repo = routed ?? pick.repo;
  const name = repos?.find((r) => r.repo_root === repo)?.name ?? repo?.split("/").filter(Boolean).pop() ?? "this project";
  return (
    <section className="flex min-h-0 flex-col gap-s3" aria-label="Project settings" data-testid="project-settings">
      <PageHeader title={`Project settings: ${name}`} lede="This repo's checks and rules, and the settings it sets instead of yours.">
        {!routed && <RepoSelect {...pick} />}
        <button type="button" className={`${quiet} text-xs`} onClick={onSettings}>
          ‹ Settings
        </button>
      </PageHeader>
      {repos && repos.length === 0 && <p className="text-ink-3">{NO_REPO}</p>}
      {repo && <Project key={repo} client={client} repo={repo} />}
    </section>
  );
}
