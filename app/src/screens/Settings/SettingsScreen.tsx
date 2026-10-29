/* Settings: this machine's brindle, in sections (About last) with a switch at
   the top: "You" and the repo's name (the session's, or one picked from the
   switch's menu when there are several). The repo side is RepoSettings
   (ProjectSettingsScreen.tsx): the same rows with where each value comes
   from. The You side has a small nav: Your settings (editable, saved to
   ~/.brindle/config.json for every project), Permissions (how Claude's permission
   requests are handled, your permission rules), Providers (one row per
   provider CLI, with how it connects, its state and its buttons) and
   Engine & health (the engine's version and update, the command-line tool,
   and the doctor as a short health list with a fix where the app has one).

   Providers (design/connect/ step 8, with the owner's final model): brindle
   detects each CLI (GET /v1/providers/installed) and uses it as-is; the CLI
   keeps itself up to date, so there is nothing here about updates. A row
   says where the CLI was found and its version, or that it isn't
   installed, and whether it is signed in. Connect runs the maker's
   official installer as-is (POST /v1/providers/{p}/install, a job whose
   lines stream into the row's progress card), then the rows are read
   again (the card stays until that look succeeds, with Look again if it
   fails); Sign in is the CLI's own browser login; Remove from brindle
   (DELETE /v1/providers/{p}) turns it off, never uninstalling it, and
   Connect on a provider that is off turns it back on with the copy still
   found (POST /v1/providers/{p}/use-mine), no installer run. The rows and installs are the app's (store/providers), so a problem
   also reaches the sidebar's status line, which opens this section.
   Each row also says how the provider connects (GET
   /v1/providers/{p}/connection; components/ProviderConnect): its own
   sign-in (Sign in, Sign out), an API key (••••last4, verified or rejected
   and when, rejected in the fail colour naming the agents it stopped; Test
   POST /v1/keys/{name}/test, Replace, Remove DELETE /v1/keys/{name}) or a
   cloud (the route and its settings, the login's time left; Renew login
   POST .../cloud-sign-in), and Change method offers the three choices
   again. A key the shell exports but brindle doesn't keep is offered for
   Import (POST /v1/keys/{name}/import).

   Your settings (GET /v1/config's `user`, with `overrides` saying which keys
   a project's files set) are edited as a draft and saved with PATCH /v1/config
   (scope user). The Claude
   permission mode is the person's own (GET and PATCH /v1/user-settings) and
   saves as soon as it is picked. Keys show as their last four characters
   only; setting one is pasted into a password field and stored write-only
   (PUT /v1/keys/{name}, routes_actions.put_key): the value never comes back.
   A provider's sign-in is the browser-first card (components/ProviderSignIn,
   POST /v1/providers/{p}/login) and the engine's update the background
   progress card (components/EngineUpgrade: POST /v1/engine/upgrade's job,
   followed on the bus). No terminal is shown anywhere here, and how each
   went comes from the engine's answer or its job events, never from a
   pane. The doctor (GET /v1/doctor) is read once per repo and on demand;
   its update line feeds the engine row (its sign-in lines stay out of the
   health list: the provider rows say that).
   Organization (OrgApprovals.tsx, read only when opened): the organization's
   members, roles and approval limits (/v1/org-permissions). The command-line
   tool's install is the shell's (install_cli, api/tauri.ts): App passes it
   in the app, where the row then says where the link went; in a browser
   tab there is none and the button stays disabled. Staff alone (GET
   /v1/debug's `staff`) get one section before About, Staff only, marked as
   not public: Debug logging, one switch (PUT /v1/debug) for uploading debug
   logs to brindle (api/debug.ts). In the app staff also get App update: one
   Update button that checks this build's feed (a release candidate's rc
   channel) and installs what it finds, then offers a restart. It is how a
   release candidate updates at all: an rc build never checks by itself
   (src-tauri/src/updates.rs). Beside it, staff can install the release
   branch's latest build, before it ships, and restart into it
   (ReleaseBuildUpdate), and go back to stable (Return to stable).
   Last, About: the app's version and its license files (About.tsx). */

import { useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { ClaudePermissionMode, Config, DaemonClient, DebugState, DoctorCheck, KeyInfo, Meta, ProviderConnection, ProviderInstalled, UserSettings } from "../../api/client";
import { debugState } from "../../api/debug";
import { connected, factsOf, settingsNote, turnedOff } from "../../api/providers";
import type { Install } from "../../api/providersInstall";
import type { About as AboutInfo, AppUpdater, AppUpdates, CliLink, ReleaseBuildUpdater } from "../../api/tauri";
import { plain } from "../../api/wording";
import { UpgradeCard, useEngineUpgrade } from "../../components/EngineUpgrade";
import { PageHeader } from "../../components/PageHeader";
import { ProgressCard } from "../../components/ProgressCard";
import { ago, ApiKeyPaste, CLOUD_NAME, CloudSetup, KEYS, MethodChoice, MODEL_PROVIDERS, ModelProviderPicker, PROVIDER_KEYS, ReadyNote, RenewLogin, useLock, type Lock, type Method } from "../../components/ProviderConnect";
import { ProviderSignIn } from "../../components/ProviderSignIn";
import { ThemeToggle } from "../../components/ThemeToggle";
import { About } from "./About";
import { OrgApprovals } from "./OrgApprovals";
import { Permissions } from "./Permissions";
import { ReleaseBuildUpdate } from "./ReleaseBuildUpdate";
import { NO_REPO, useRepo } from "../../components/RepoSelect";
import { RepoSettings } from "./ProjectSettingsScreen";
import { alert, button, card, field, h2, main, mono, notice, pill, quiet } from "../../components/ui";
import { useProviders } from "../../store/providers";
import { StoreContext, useAppState } from "../../store/useStore";
import {
  MARK,
  SETTINGS,
  YOURS,
  carryEdits,
  changed,
  draftOf,
  fixFor,
  inSignInRows,
  invalid,
  isOptional,
  listOf,
  listText,
  updateNotice,
  userLines,
  values,
  type Draft,
  type Fix,
  type Setting,
} from "./settings";

export interface SettingsScreenProps {
  client: DaemonClient;
  /* Puts the `brindle` command on the PATH (the shell's install_cli,
     api/tauri.ts, which answers with the link it made). Without one (a
     browser tab) the button is disabled. */
  installCli?: () => Promise<CliLink | void>;
  /* The app's own Update (the shell's app_update_install and app_restart,
     api/tauri.ts), shown to staff only. Without one (a browser tab) there is
     no such row. */
  appUpdater?: AppUpdater;
  /* Staff's install of the release branch's latest build (the shell's
     app_release_build_install, api/tauri.ts): a row under App update. */
  releaseBuildUpdater?: ReleaseBuildUpdater;
  /* About's license files (the shell's about_read, api/tauri.ts). Without
     one (a browser tab) About only names them. */
  readAbout?: () => Promise<AboutInfo>;
  /* About's updates for a build that updates by itself: last checked, Check
     now, Restart to update (the shell's, api/tauri.ts AppUpdates). */
  appUpdates?: AppUpdates;
  /* The section to scroll to on opening: "providers" from the sidebar's problem line, "permissions" from the Inbox. */
  section?: "providers" | "permissions";
  /* A new value asks to scroll to `section` again, the page already open. */
  ask?: number;
  /* A permission rule to open in its editor (the Inbox's "Open the deny rule"). */
  rule?: string;
  /* The repo to open the switch on (the sidebar's repo header, the old project-settings route): its view, not You's. */
  repo?: string;
  /* The current session's repo: the one the switch starts on when no route names one. */
  current?: string;
}

export const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

const SECTIONS = [
  { id: "your-settings", label: "Your settings" },
  { id: "permissions", label: "Permissions" },
  { id: "providers", label: "Providers" },
  { id: "engine", label: "Engine & health" },
  { id: "about", label: "About" },
];

/* The staff section sits before About, and is in the nav only for staff. */
const STAFF = { id: "staff", label: "Staff only" };

/* The small in-page nav: one button per section, scrolling to it. */
function SectionNav({ staff }: { staff: boolean }) {
  const shown = staff ? [...SECTIONS.slice(0, -1), STAFF, ...SECTIONS.slice(-1)] : SECTIONS;
  return (
    <nav aria-label="Settings sections" className="flex flex-wrap gap-1">
      {shown.map((s) => (
        <button
          key={s.id}
          type="button"
          className={`${quiet} rounded-sm border border-line px-s2 text-xs`}
          onClick={() => document.getElementById(s.id)?.scrollIntoView?.({ behavior: "smooth", block: "start" })}
        >
          {s.label}
        </button>
      ))}
    </nav>
  );
}

/* A section: its heading and one-line blurb, actions on the right, and a card of rows. */
export function Section({ id, title, blurb, actions, children, testId, staff = false }: { id: string; title: string; blurb?: ReactNode; actions?: ReactNode; children: ReactNode; testId: string; staff?: boolean }) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="flex scroll-mt-s2 flex-col gap-s1" data-testid={testId}>
      <header className="flex flex-wrap items-baseline gap-x-s2 gap-y-0.5">
        <h2 id={`${id}-title`} className={h2}>
          {title}
        </h2>
        {staff && <span className={`${pill} text-sap`}>STAFF</span>}
        {blurb && <span className="text-xs text-ink-3">{blurb}</span>}
        <span className="flex-1" />
        {actions}
      </header>
      <div className={`${card} flex flex-col divide-y divide-line`}>{children}</div>
    </section>
  );
}

/* One row of a section: a label with its one-line help on the left, the
   control (or the state and one button) on the right; `wide` puts the
   control under the label, for a list. */
function Row({ label: text, help, htmlFor, badges, wide = false, children, testId, data }: { label: ReactNode; help?: ReactNode; htmlFor?: string; badges?: ReactNode; wide?: boolean; children?: ReactNode; testId?: string; data?: Record<string, string | undefined> }) {
  const heading = (
    <>
      {text}
      {badges}
    </>
  );
  const headingClass = "flex flex-wrap items-center gap-x-s1 text-sm font-medium";
  return (
    <div className={`grid gap-x-s3 gap-y-1 px-s2 py-s1 ${wide ? "grid-cols-1" : "grid-cols-[minmax(0,1fr)_auto] items-center"}`} data-testid={testId} {...Object.fromEntries(Object.entries(data ?? {}).map(([k, v]) => [`data-${k}`, v]))}>
      <div className="min-w-0">
        {htmlFor ? (
          <label htmlFor={htmlFor} className={headingClass}>
            {heading}
          </label>
        ) : (
          <div className={headingClass}>{heading}</div>
        )}
        {help && <p className="text-xs text-ink-3">{help}</p>}
      </div>
      {children && <div className={`flex min-w-0 flex-wrap items-center gap-s1 ${wide ? "" : "justify-end"}`}>{children}</div>}
    </div>
  );
}

const Edited = () => <span className={`${pill} text-ink-2`}>edited</span>;

/* On or off, as a switch. */
function Toggle({ id, label: name, checked, disabled, onChange }: { id: string; label: string; checked: boolean; disabled: boolean; onChange(on: boolean): void }) {
  return (
    <button
      id={id}
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={name}
      disabled={disabled}
      className={`relative h-5 w-9 shrink-0 rounded-full border transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${checked ? "border-pass bg-pass" : "border-line-strong bg-surface-2"}`}
      onClick={() => onChange(!checked)}
    >
      <span aria-hidden className={`absolute top-0.5 size-3.5 rounded-full transition-[left] ${checked ? "left-[18px] bg-canvas" : "left-0.5 bg-ink-3"}`} />
    </button>
  );
}

/* One entry per line, kept as typed (spaces, a trailing newline) and read
   as the engine's list on each change; the text follows the draft only
   when the draft moves away from it (a discard, a reload). */
function ListField({ s, value, disabled, onChange }: { s: Setting; value: unknown; disabled: boolean; onChange(value: unknown): void }) {
  const [text, setText] = useState(() => listText(value));
  useEffect(() => {
    setText((t) => (JSON.stringify(listOf(t)) === JSON.stringify(value) ? t : listText(value)));
  }, [value]);
  return (
    <textarea
      id={`setting-${s.key}`}
      className={`${field} min-h-16 w-full resize-y font-mono`}
      aria-label={s.label}
      value={text}
      disabled={disabled}
      rows={Math.max(2, text.split("\n").length + 1)}
      placeholder="npm test"
      onChange={(e) => {
        setText(e.target.value);
        onChange(listOf(e.target.value));
      }}
    />
  );
}

function Field({ s, value, disabled, onChange }: { s: Setting; value: unknown; disabled: boolean; onChange(value: unknown): void }) {
  const id = `setting-${s.key}`;
  switch (s.kind) {
    case "list":
      return <ListField s={s} value={value} disabled={disabled} onChange={onChange} />;
    case "number":
      return (
        <>
          <input
            id={id}
            type="number"
            min={0}
            required
            className={`${field} w-20 text-right ${typeof value !== "number" ? "border-sap" : ""}`}
            aria-label={s.label}
            aria-invalid={typeof value !== "number" || undefined}
            value={typeof value === "number" ? value : ""}
            disabled={disabled}
            onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))}
          />
          {s.unit && <span className="text-xs text-ink-3">{s.unit}</span>}
        </>
      );
    case "text":
      return <input id={id} type="text" className={`${field} w-56`} aria-label={s.label} value={typeof value === "string" ? value : ""} disabled={disabled} onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)} />;
    case "boolean":
      return <Toggle id={id} label={s.label} checked={value === true} disabled={disabled} onChange={onChange} />;
    case "choice":
      return (
        <select id={id} className={`${field} max-w-72`} aria-label={s.label} value={typeof value === "string" ? value : ""} disabled={disabled} onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}>
          {(s.choices ?? []).map((c) => (
            <option key={c.value ?? ""} value={c.value ?? ""}>
              {c.label}
            </option>
          ))}
        </select>
      );
  }
}

export function SettingRow({ s, value, dirty, disabled, onChange, override }: { s: Setting; value: unknown; dirty: boolean; disabled: boolean; onChange(value: unknown): void; override?: ReactNode }) {
  return (
    <Row
      label={s.label}
      help={
        override ? (
          <>
            {s.blurb}
            {override}
          </>
        ) : (
          s.blurb
        )
      }
      htmlFor={`setting-${s.key}`}
      wide={s.kind === "list"}
      badges={
        <>
          {s.pro && <span className={`${pill} text-ink-2`}>Pro</span>}
          {dirty && <Edited />}
        </>
      }
      data={{ setting: s.key }}
    >
      <Field s={s} value={value} disabled={disabled} onChange={onChange} />
    </Row>
  );
}

/* A disclosure under a card's rows: More / Less. */
export function More({ open, onToggle, label: text = "More", count }: { open: boolean; onToggle(): void; label?: string; count?: number }) {
  return (
    <button type="button" className={`${quiet} self-start text-xs`} aria-expanded={open} onClick={onToggle}>
      {open ? "Less" : text}
      {count !== undefined && !open ? ` (${count})` : ""} {open ? "▴" : "▾"}
    </button>
  );
}

/* The settings from the config the screen doesn't edit, as key: value lines. */
export function RestOfConfig({ lines, testId, note }: { lines: [string, string][]; testId: string; note: string }) {
  return (
    <div className="flex flex-col gap-1">
      <dl className="grid grid-cols-[auto_1fr] gap-x-s2 gap-y-0.5 text-xs" data-testid={testId}>
        {lines.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className={`${mono} text-ink-3`}>{k}</dt>
            <dd className={`${mono} min-w-0 break-all`}>{v}</dd>
          </div>
        ))}
      </dl>
      <p className="text-xs text-ink-3">{note}</p>
    </div>
  );
}

/* The person's choice of how Claude's permission requests are handled,
   saved the moment it is picked (PATCH /v1/user-settings). */
const MODES: { value: ClaudePermissionMode; label: string; blurb: string }[] = [
  { value: "default", label: "Normal", blurb: "Claude's held permission requests show up in the Inbox for you to answer." },
  { value: "auto", label: "Auto", blurb: "Claude approves most commands itself; only what it can't decide reaches you." },
];

function PermissionMode({ client }: { client: DaemonClient }) {
  const [settings, setSettings] = useState<UserSettings | null>(null);
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    client.userSettings().then(
      (s) => live && setSettings(s),
      (e: unknown) => live && setUnavailable(message(e)),
    );
    return () => {
      live = false;
    };
  }, [client]);
  const pick = async (mode: ClaudePermissionMode) => {
    if (!settings || saving || settings.claude_permission_mode === mode) return;
    setSaving(true);
    setError(null);
    try {
      setSettings(await client.patchUserSettings({ claude_permission_mode: mode }));
    } catch (e) {
      setError(message(e));
    } finally {
      setSaving(false);
    }
  };
  const current = settings?.claude_permission_mode;
  return (
    <Row
      label="Claude permission mode"
      help={unavailable ? `Not offered by this engine: ${unavailable}` : (MODES.find((m) => m.value === current)?.blurb ?? "How Claude's permission requests are handled.")}
      testId="permission-mode"
      data={{ mode: current }}
    >
      {!unavailable && (
        <div role="radiogroup" aria-label="Claude permission mode" className="inline-flex rounded-md border border-line p-0.5">
          {MODES.map((m) => (
            <button
              key={m.value}
              type="button"
              role="radio"
              aria-checked={current === m.value}
              disabled={saving || !settings}
              title={m.blurb}
              className={`rounded-sm px-s2 py-0.5 text-sm disabled:opacity-50 ${current === m.value ? "bg-pass-soft font-medium text-pass" : "text-ink-2 hover:bg-surface-2"}`}
              onClick={() => void pick(m.value)}
            >
              {m.label}
            </button>
          ))}
        </div>
      )}
      {error && (
        <p role="alert" className={`${alert} w-full`}>
          Could not save: {error}
        </p>
      )}
    </Row>
  );
}

/* Staff-only debug logging (owner decision): shown only when GET /debug
   says this account is staff; anyone else, or an engine without the route,
   sees nothing. One switch (PUT /debug), and what it does. */
function DebugLogging({ client }: { client: DaemonClient }) {
  const [state, setState] = useState<DebugState | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    client.debug().then(
      (s) => {
        if (!live) return;
        setState(s);
        debugState(client, s);
      },
      () => {},
    );
    return () => {
      live = false;
    };
  }, [client]);
  if (!state?.staff) return null;
  const turn = async (on: boolean) => {
    setSaving(true);
    setError(null);
    try {
      const s = await client.setDebug(on);
      setState(s);
      debugState(client, s);
    } catch (e) {
      setError(message(e));
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="contents" data-testid="debug-logging">
      <Row label="Upload debug logs" htmlFor="debug-logging" help="While this is on, brindle's and this app's debug logs upload to brindle." testId="debug-row">
        <Toggle id="debug-logging" label="Debug logging" checked={state.enabled} disabled={saving} onChange={(on) => void turn(on)} />
      </Row>
      {error && (
        <p role="alert" className={`${alert} m-s2`}>
          Could not save: {error}
        </p>
      )}
    </div>
  );
}

/* Staff-only app update (owner decision): a release candidate never updates
   by itself, so staff update it here, and can install the release branch's
   latest build (ReleaseBuildUpdate). Shown only when GET /debug says this
   account is staff and the shell gives an updater; anyone else sees nothing.
   The shell checks staff again before installing. */
function AppUpdate({ client, updater, release, readAbout }: { client: DaemonClient; updater?: AppUpdater; release?: ReleaseBuildUpdater; readAbout?: () => Promise<AboutInfo> }) {
  const [staff, setStaff] = useState(false);
  /* This build's version, for Return to stable (shown on a release-branch build only). */
  const [version, setVersion] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    if (release && readAbout)
      readAbout().then(
        (a) => {
          if (live) setVersion(a.version);
        },
        () => {},
      );
    return () => {
      live = false;
    };
  }, [release, readAbout]);
  const [state, setState] = useState<"idle" | "checking" | "latest" | "installed" | "restarting">("idle");
  const [installed, setInstalled] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    client.debug().then(
      (s) => {
        if (live) setStaff(s.staff);
      },
      () => {},
    );
    return () => {
      live = false;
    };
  }, [client]);
  if (!staff || (!updater && !release)) return null;
  const update = async (updater: AppUpdater) => {
    setState("checking");
    setError(null);
    try {
      const version = await updater.install();
      setInstalled(version);
      setState(version ? "installed" : "latest");
    } catch (e) {
      setError(message(e));
      setState("idle");
    }
  };
  const restart = async (updater: AppUpdater) => {
    setState("restarting");
    try {
      await updater.restart();
    } catch (e) {
      setError(message(e));
      setState("installed");
    }
  };
  const help =
    state === "installed"
      ? `brindle ${installed} is installed. Restart to use it.`
      : state === "latest"
        ? "This is the latest build."
        : "Checks for a newer build of this app (a beta checks the beta channel) and installs it.";
  return (
    <div className="contents" data-testid="app-update">
      {updater && (
        <Row label="Update the app" help={help} testId="app-update-row">
          {state === "installed" || state === "restarting" ? (
            <button type="button" className={button} disabled={state === "restarting"} onClick={() => void restart(updater)}>
              Restart
            </button>
          ) : (
            <button type="button" className={button} disabled={state === "checking"} onClick={() => void update(updater)}>
              {state === "checking" ? "Updating…" : "Update"}
            </button>
          )}
        </Row>
      )}
      {release && <ReleaseBuildUpdate updater={release} version={version} />}
      {error && (
        <p role="alert" className={`${alert} m-s2`}>
          Could not update: {error}
        </p>
      )}
    </div>
  );
}

/* The key a provider runs on: OpenCode's model provider's own (`model`),
   any other provider's first stored one. */
const keyOf = (provider: string, keys: KeyInfo[] | null, model?: string): KeyInfo | undefined =>
  (model ? [model] : (PROVIDER_KEYS[provider] ?? [])).map((n) => keys?.find((k) => k.name === n && k.set)).find(Boolean);

/* OpenCode's model providers that take a key: the one shown first is the first with a key stored. */
const KEY_MODELS = MODEL_PROVIDERS.filter((m) => m.key);
const firstModel = (keys: KeyInfo[] | null) => KEY_MODELS.find((m) => keys?.some((k) => k.name === m.key && k.set)) ?? KEY_MODELS[0];

/* Keys no provider reads (a profile's api_key_env): kept, in a row of their own. */
const PROVIDER_KEY_NAMES = new Set(Object.values(PROVIDER_KEYS).flat());
const otherKey = (k: KeyInfo) => !k.provider && !PROVIDER_KEY_NAMES.has(k.name);

/* "40 min", "3h", "2d" until `ts` (seconds). */
function until(ts: number, now = Date.now() / 1000): string {
  const m = Math.max(0, Math.round((ts - now) / 60));
  return m < 60 ? `${m} min` : m < 1440 ? `${Math.floor(m / 60)}h` : `${Math.floor(m / 1440)}d`;
}

/* The agents that stopped on this provider's credentials (the engine's stopped_agents), by name. */
const stoppedOn = (agents: { name: string }[]) => (agents.length ? ` Stopped ${agents.length === 1 ? "1 agent" : `${agents.length} agents`}: ${agents.map((a) => a.name).join(", ")}.` : "");

/* How a key or a cloud connection is doing, in one line: verified with when,
   the cloud login's time left, or what's wrong in the fail colour, naming
   the agents it stopped. `keyLine`: the key's line on a row left to auto. */
function Health({ connection, keyInfo, keyLine = false }: { connection: ProviderConnection; keyInfo: KeyInfo | undefined; keyLine?: boolean }) {
  const stopped = connection.stopped_agents ?? [];
  const line = (tone: "pass" | "sap" | "fail" | "ink-3", text: string) => (
    <span className={`flex items-center gap-1 ${tone === "fail" ? "text-fail" : ""}`} data-testid="provider-health" data-tone={tone} data-stopped={stopped.length}>
      <span className={`size-2 shrink-0 rounded-full ${tone === "pass" ? "bg-pass" : tone === "sap" ? "bg-sap" : tone === "fail" ? "bg-fail" : "bg-ink-3"}`} aria-hidden />
      {text}
    </span>
  );
  if (connection.method === "api_key" || keyLine) {
    if (!keyInfo) return line("sap", "No key stored: Replace adds one.");
    const billing = KEYS[keyInfo.name]?.billing.replace(/^B/, "b");
    if (keyInfo.status === "rejected") return line("fail", `Rejected ${ago(keyInfo.checked_at)}: ${KEYS[keyInfo.name]?.maker ?? "the provider"} refused this key.${stoppedOn(stopped)}`);
    if (keyInfo.status === "verified") return line("pass", `Verified ${ago(keyInfo.checked_at)}${billing ? ` · ${billing}` : ""}`);
    return line("ink-3", "Saved, not checked yet");
  }
  const login = connection.login;
  if (login?.valid) {
    if (login.expires_at == null) return line("pass", "Cloud login valid");
    return line(login.expires_at - Date.now() / 1000 < 3600 ? "sap" : "pass", `Cloud login expires in ${until(login.expires_at)}`);
  }
  if (login?.expired) return line("fail", `Cloud login expired.${stoppedOn(stopped)}`);
  return line("ink-3", `Not signed in to ${CLOUD_NAME[connection.cloud ?? ""] ?? "the cloud"} yet`);
}

/* Change method: the three choices (Your cloud only where the engine has a
   route), and the chosen one's setup, in the row. Sign in is saved as soon
   as it is picked, over auto too, so a stored or exported key stops billing. */
function ChangeMethod({ client, row, connection, keys, repo, model: rowModel, onModel, lock, onChanged, onClose }: {
  client: DaemonClient;
  row: ProviderInstalled;
  connection: ProviderConnection;
  keys: KeyInfo[] | null;
  repo: string | null;
  /* OpenCode: the row's model provider, where this starts, and where a key saved here moves it. */
  model: (typeof MODEL_PROVIDERS)[number];
  onModel(model: (typeof MODEL_PROVIDERS)[number]): void;
  /* The row's write lock: a Sign in, a key or a cloud route saved here holds it until it settles. */
  lock: Lock;
  onChanged(): void;
  onClose(): void;
}) {
  const f = factsOf(row);
  const saved = connection.method;
  const [choice, setChoice] = useState<Method | null>(saved === "api_key" || saved === "cloud" || saved === "sign_in" ? saved : null);
  const [error, setError] = useState<string | null>(null);
  const opencode = row.provider === "opencode";
  const [model, setModel] = useState(rowModel);
  const cloud = (connection.clouds ?? []).length > 0;
  const busy = lock.busy;
  const done = () => {
    onChanged();
    onClose();
  };
  const pick = async (m: Method) => {
    if (busy) return;
    setChoice(m);
    setError(null);
    if (m !== "sign_in" || saved === "sign_in") return;
    const got = await lock.run(() => client.setProviderConnection(row.provider, { method: "sign_in", ...(repo ? { repo } : {}) }).then(() => null, (e: unknown) => message(e)));
    if (got) setError(got);
    else done();
  };
  return (
    <div className="flex w-full flex-col gap-s2 pt-s1" data-testid={`change-method-${row.provider}`}>
      {opencode && (
        <ModelProviderPicker
          value={model.id}
          disabled={busy}
          onChange={(id) => {
            const next = MODEL_PROVIDERS.find((x) => x.id === id) ?? MODEL_PROVIDERS[0];
            setModel(next);
            if (next.cloud && cloud) setChoice("cloud");
          }}
        />
      )}
      <MethodChoice provider={row.provider} name={f.name} value={choice} cloud={cloud} disabled={busy} onChange={(m) => void pick(m)} />
      {choice === "api_key" &&
        (opencode && !model.key ? (
          <p className="text-xs text-ink-3">{model.name} connects through your cloud, not a key.</p>
        ) : (
          <ApiKeyPaste
            key={model.id}
            client={client}
            provider={row.provider}
            expect={(opencode ? model.key : undefined) ?? PROVIDER_KEYS[row.provider]?.[0] ?? "ANTHROPIC_API_KEY"}
            keys={keys}
            repo={repo}
            lock={lock}
            onSaved={() => {
              // The row's key controls follow the model provider whose key was just saved.
              if (opencode && model.key) onModel(model);
              done();
            }}
            onCancel={onClose}
          />
        ))}
      {choice === "cloud" && cloud && <CloudSetup client={client} provider={row.provider} name={f.name} route={opencode ? model.cloud : undefined} repo={repo} lock={lock} onSignedIn={done} />}
      {error && (
        <p className={alert} role="alert">
          Couldn't change how {f.name} connects: {error}
        </p>
      )}
    </div>
  );
}

type RowState = "signed_in" | "signed_out" | "unknown" | "not_installed" | "off";
const DOT: Record<RowState, string> = { signed_in: "bg-pass", signed_out: "bg-sap", unknown: "bg-ink-3", not_installed: "bg-ink-3", off: "bg-ink-3" };
const STATE_TEXT: Record<RowState, string> = { signed_in: "Signed in", signed_out: "Not signed in", unknown: "Sign-in not checked", not_installed: "Not installed", off: "Removed from brindle" };
const SIGN_IN_HELP: Record<RowState, string> = {
  signed_in: "Your account is connected.",
  signed_out: "Sign in with your account to use this provider. Continue in your browser.",
  unknown: "We couldn't confirm your sign-in. Sign in to connect your account.",
  not_installed: "Install this provider, then sign in with your account.",
  off: "Connect to use this provider in brindle again.",
};

function rowState(row: ProviderInstalled): RowState {
  if (connected(row)) return row.signed_in === true ? "signed_in" : row.signed_in === false ? "signed_out" : "unknown";
  return turnedOff(row) ? "off" : "not_installed";
}

/* A provider's row (design/connect/ step 8, the owner's final model): where
   brindle found the CLI and its version, or that it isn't installed; signed
   in or not. Connect runs the maker's official installer as-is, its lines
   streaming into the calm progress card (a failure stays there with Retry,
   and in the sidebar's status line, until dismissed); Sign in is the CLI's
   own browser login (the browser-first card); Remove from brindle stops
   brindle using it and leaves it installed. No update controls: each CLI
   keeps itself up to date. */
function ProviderRowView({ client, repo, connection, keys, onChanged, row, install, detectError, check, onInstall, onTurnOn, onDismiss, onRemove, onSignOut, onLookAgain }: {
  client: DaemonClient;
  repo: string | null;
  /* How it connects (GET /providers/{p}/connection: its readiness and the agents stopped on it); null until read, or from an engine without the route. */
  connection: ProviderConnection | null;
  /* GET /config's keys. */
  keys: KeyInfo[] | null;
  /* The connection or a key changed here: read both again. */
  onChanged(): void;
  row: ProviderInstalled;
  install: Install | undefined;
  /* Why the rows couldn't be read again (after a finished install, its row is stale until they are). */
  detectError: string | null;
  check(): Promise<boolean>;
  onInstall(): void;
  onTurnOn(path: string): Promise<void>;
  onDismiss(): void;
  onRemove(): Promise<void>;
  onSignOut(): Promise<void>;
  onLookAgain(): void;
}) {
  const f = factsOf(row);
  const state = rowState(row);
  const on = connected(row);
  const [signing, setSigning] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [busy, setBusy] = useState<"removing" | "connecting" | "signing-out" | "testing" | "removing-key" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  // One editor at a time: Change method or Replace, never both.
  const [editor, setEditor] = useState<"change" | "replace" | null>(null);
  const changing = editor === "change";
  const replacing = editor === "replace";
  const setReplacing = (on: boolean) => setEditor(on ? "replace" : null);
  /* The row's connection writes (a key saved, a method chosen, a key removed): while one is in
     flight nothing else in the row can write, open, close or switch, whichever form started it. */
  const lock = useLock();
  const locked = lock.busy;
  const installing = install?.status === "starting" || install?.status === "running";
  // A key or a cloud, as saved; its own sign-in (sign_in); auto, a key when one is set, else
  // its sign-in; or the org's say.
  const method = connection?.method;
  const viaKey = method === "api_key";
  const viaCloud = method === "cloud" && !!connection?.cloud;
  const auto = !!connection && method === "auto";
  const opencode = row.provider === "opencode";
  const [model, setModel] = useState(firstModel(keys));
  const keyInfo = keyOf(row.provider, keys, opencode ? model.key : undefined);
  // A stored key stays manageable whether it was chosen here, imported, or set before (auto).
  const keyed = viaKey || (auto && (!!keyInfo || (opencode && KEY_MODELS.some((m) => keyOf(row.provider, keys, m.key)))));
  const act = async (kind: "removing" | "connecting" | "signing-out" | "testing" | "removing-key", run: () => Promise<void>, what: string) => {
    setBusy(kind);
    setActionError(null);
    try {
      await run();
    } catch (e) {
      setActionError(`Couldn't ${what} ${f.name}: ${message(e)}`);
    } finally {
      setBusy(null);
    }
  };
  const remove = () => act("removing", onRemove, "remove");
  // Brindle's own copy only; offered where the engine can sign it out.
  const canSignOut = row.provider === "claude" || row.provider === "codex";
  // An off provider still found comes back as it is; one missing runs the maker's installer.
  const connectNow = () => (turnedOff(row) && row.path ? void act("connecting", () => onTurnOn(row.path!), "connect") : onInstall());
  const installAction = turnedOff(row) && row.path ? "Connect" : "Install";
  const keyName = (opencode ? model.key : undefined) ?? keyInfo?.name ?? PROVIDER_KEYS[row.provider]?.[0];
  const testKey = () => act("testing", async () => void (await client.testKey(keyInfo!.name), onChanged()), "test the key of");
  // The key goes; chosen as the method, the method goes too and the provider is back on auto.
  const removeKey = () =>
    act(
      "removing-key",
      async () => {
        await lock.run(async () => {
          if (keyInfo) await client.deleteKey(keyInfo.name);
          if (viaKey) await client.setProviderConnection(row.provider, { method: "auto", ...(repo ? { repo } : {}) });
        });
        onChanged();
      },
      "remove the key of",
    );
  const ready = connection?.ready;
  const help = (
    <>
      {(viaKey || viaCloud) && connection ? (
        <Health connection={connection} keyInfo={keyInfo} />
      ) : method === "org" && connection?.managed ? (
        <span className="block">{plain(connection.managed)}</span>
      ) : auto ? (
        <>
          <span className="block" data-testid="provider-auto">
            Uses your key if set, else {f.name}'s own sign-in.
          </span>
          {keyed && <Health connection={connection!} keyInfo={keyInfo} keyLine />}
        </>
      ) : (
        <span className="block">{SIGN_IN_HELP[state]}</span>
      )}
      {on && connection && ready && !ready.ok && !(viaCloud && ready.action === "renew") && <ReadyNote client={client} connection={connection} onSetKey={() => setReplacing(true)} onChanged={onChanged} />}
      <button type="button" className="mt-1 underline underline-offset-2 hover:text-ink" aria-expanded={expanded} aria-controls={`provider-details-${row.provider}`} onClick={() => setExpanded((value) => !value)}>
        Connection details
      </button>
      {expanded && (
        <span id={`provider-details-${row.provider}`} className="mt-s1 block break-words">
          {on ? (
            <>
              Found at <span className={mono}>{row.path}</span>
              {row.version && <> · {row.version}</>}. {settingsNote(row)}
            </>
          ) : turnedOff(row) && row.path ? (
            <>
              Still installed at <span className={mono}>{row.path}</span>; brindle doesn't use it. Connect brings it back.
            </>
          ) : (
            <>Connect runs {f.maker}'s official installer{f.installer.label && <> ({f.installer.label})</>}, as-is.</>
          )}
          {on && !signing && (
            <button type="button" className={`${quiet} block`} aria-label={`Remove ${f.name} from brindle`} disabled={busy !== null} onClick={() => void remove()}>
              {busy === "removing" ? "Removing…" : "Remove from brindle"}
            </button>
          )}
        </span>
      )}
      {actionError && (
        <span className="block text-sap" role="alert">
          {actionError}
        </span>
      )}
    </>
  );
  return (
    <Row
      label={f.name}
      help={help}
      wide={signing || !!install || changing || replacing}
      testId={`provider-${row.provider}`}
      data={{ provider: row.provider, state, install: install?.status, method: method ?? "auto" }}
      badges={
        viaKey ? (
          <span className="inline-flex items-center gap-1 text-xs font-normal text-ink-2" data-testid="provider-method">
            API key {keyInfo ? <span className={mono}>••••{keyInfo.last4}</span> : "· none stored"}
          </span>
        ) : viaCloud ? (
          <span className="inline-flex flex-wrap items-center gap-1 text-xs font-normal text-ink-2" data-testid="provider-method">
            {CLOUD_NAME[connection!.cloud!] ?? connection!.cloud}
            {Object.values(connection!.settings ?? {}).filter(Boolean).length > 0 && <span className={mono}>· {Object.values(connection!.settings ?? {}).filter(Boolean).join(" · ")}</span>}
          </span>
        ) : (
          <span className={`inline-flex items-center gap-1 text-xs font-normal ${state === "signed_in" ? "text-pass" : "text-ink-2"}`} data-testid="provider-state">
            <span className={`size-2 rounded-full ${DOT[state]}`} aria-hidden />
            {STATE_TEXT[state]}
            {auto && keyInfo && <span className={`${mono} text-ink-2`}>· key ••••{keyInfo.last4}</span>}
          </span>
        )
      }
    >
      {!on && !install && (
        <button type="button" className={button} aria-label={`${installAction} ${f.name}`} disabled={busy !== null} onClick={connectNow}>
          {busy === "connecting" ? "Connecting…" : installAction}
        </button>
      )}
      {on && keyed && opencode && !changing && (
        <label className="flex items-center gap-s1 text-xs text-ink-2">
          Model provider
          <select
            className={field}
            value={model.id}
            onChange={(e) => {
              setModel(KEY_MODELS.find((m) => m.id === e.target.value) ?? KEY_MODELS[0]);
              setReplacing(false);
            }}
          >
            {KEY_MODELS.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </select>
        </label>
      )}
      {on && keyed && !replacing && (
        <>
          {keyInfo && (
            <button type="button" className={button} disabled={busy !== null || locked} onClick={() => void testKey()}>
              {busy === "testing" ? "Testing…" : "Test"}
            </button>
          )}
          <button type="button" className={button} disabled={busy !== null || locked} onClick={() => setReplacing(true)}>
            {keyInfo ? "Replace" : "Add a key"}
          </button>
          {(keyInfo || viaKey) && (
            <button type="button" className={button} disabled={busy !== null || locked} onClick={() => void removeKey()}>
              {busy === "removing-key" ? "Removing…" : "Remove"}
            </button>
          )}
        </>
      )}
      {on && replacing && keyName && (
        <div className="w-full">
          <ApiKeyPaste key={keyName} client={client} provider={row.provider} expect={keyName} keys={keys} repo={repo} lock={lock} onSaved={() => (setReplacing(false), onChanged())} onCancel={() => setReplacing(false)} />
        </div>
      )}
      {on && viaCloud && <RenewLogin client={client} provider={row.provider} route={connection!.cloud!} onSignedIn={onChanged} />}
      {on && !viaKey && !viaCloud && canSignOut && state === "signed_in" && !signing && (
        <button type="button" className={quiet} aria-label={`Sign out of ${f.name}`} disabled={busy !== null || locked} onClick={() => void act("signing-out", onSignOut, "sign out of")}>
          {busy === "signing-out" ? "Signing out…" : "Sign out"}
        </button>
      )}
      {on && !viaKey && !viaCloud && (row.signed_in !== true || signing) && <ProviderSignIn provider={row.provider} check={check} onActive={setSigning} details={false} />}
      {on && connection && method !== "org" && !signing && (
        <button type="button" className={quiet} aria-expanded={changing} disabled={locked} onClick={() => setEditor(changing ? null : "change")}>
          Change method {changing ? "▴" : "▾"}
        </button>
      )}
      {changing && connection && (
        <ChangeMethod
          client={client}
          row={row}
          connection={connection}
          keys={keys}
          repo={repo}
          model={model}
          onModel={(m) => setModel(KEY_MODELS.find((k) => k.id === m.id) ?? model)}
          lock={lock}
          onChanged={onChanged}
          onClose={() => setEditor(null)}
        />
      )}
      {install && (
        <div className="w-full">
          <ProgressCard
            label={`Installing ${f.name}`}
            testId={`provider-install-${row.provider}`}
            title={
              <>
                <span className="font-semibold">{f.name}</span>
                <span className="text-sm text-ink-3">{f.maker}'s official installer</span>
              </>
            }
            status={installing ? "running" : install.status}
            lines={install.lines}
            failure={{ headline: "Install failed.", message: plain(install.error ?? "") }}
            onRetry={onInstall}
            other={{ label: "Dismiss", onClick: onDismiss }}
          >
            {/* Installed; the card stays until brindle has looked for the CLI again. Looking again runs no installer. */}
            {install.status === "done" && (
              <p className="flex flex-wrap items-center gap-s1 text-sm text-ink-2" data-testid={`provider-install-${row.provider}-detect`}>
                {detectError ? (
                  <>
                    <span className="text-sap" role="alert">
                      {f.name} is installed, but brindle couldn't look for it again: {plain(detectError)}
                    </span>
                    <button type="button" className={button} onClick={onLookAgain}>
                      Look again
                    </button>
                  </>
                ) : (
                  <>{f.name} is installed. Looking for it…</>
                )}
              </p>
            )}
          </ProgressCard>
        </div>
      )}
    </Row>
  );
}

const TONE: Record<string, string> = { ok: "text-pass", warn: "text-sap", fail: "text-fail" };

/* A doctor line as a health row: its mark, name and words, and a fix where the app has one. */
function HealthRow({ c, busy, onFix }: { c: DoctorCheck; busy: boolean; onFix(fix: Fix): void }) {
  // The doctor marks a pending update ok (nothing is broken); here it is something to do.
  const level = c.name === "brindle update" ? "warn" : c.level;
  const fix = fixFor(c);
  return (
    <li className="flex items-start gap-s1 px-s2 py-1 text-sm" data-check={c.name} data-level={level}>
      <span aria-hidden className={`w-4 shrink-0 font-semibold ${TONE[level] ?? "text-ink-3"}`}>
        {MARK[level] ?? "·"}
      </span>
      <span className="w-36 shrink-0 font-medium">{c.name}</span>
      {/* The doctor's install pointers (a bare command after "Install:") stay: they are the fix. */}
      <span className="min-w-0 flex-1 break-words text-ink-2">{fix ? plain(c.detail) : c.detail}</span>
      {fix && (
        <button type="button" className={`${button} shrink-0`} disabled={busy} onClick={() => onFix(fix)}>
          Update
        </button>
      )}
    </li>
  );
}

/* The switch at the top: You, or the repo (a menu to pick another when there are several). */
function ScopeSwitch({ view, onView, repo, repos, onRepo, disabled }: { view: "you" | "repo"; onView(v: "you" | "repo"): void; repo: string | null; repos: ReturnType<typeof useRepo>["repos"]; onRepo(repo: string): void; disabled: boolean }) {
  const name = repos?.find((r) => r.repo_root === repo)?.name ?? repo?.split("/").filter(Boolean).pop();
  const choices: { id: "you" | "repo"; label: string }[] = [{ id: "you", label: "You" }, ...(repo ? [{ id: "repo" as const, label: name ?? repo }] : [])];
  const [open, setOpen] = useState(false);
  const several = !!repos && repos.length > 1;
  return (
    <div className="relative flex flex-wrap items-center gap-s1">
      <div role="radiogroup" aria-label="Settings for" className="inline-flex rounded-md border border-line p-0.5">
        {choices.map((c) => (
          <button
            key={c.id}
            type="button"
            role="radio"
            aria-checked={view === c.id}
            aria-haspopup={c.id === "repo" && several ? "menu" : undefined}
            aria-expanded={c.id === "repo" && several ? open : undefined}
            disabled={disabled}
            className={`rounded-sm px-s2 py-0.5 text-sm disabled:opacity-50 ${view === c.id ? "bg-pass-soft font-medium text-pass" : "text-ink-2 hover:bg-surface-2"}`}
            // The repo segment, already chosen, opens the menu of repos.
            onClick={() => (c.id === "repo" && several && view === "repo" ? setOpen((o) => !o) : (setOpen(false), onView(c.id)))}
          >
            {c.label}
            {c.id === "repo" && several ? " ▾" : ""}
          </button>
        ))}
      </div>
      {open && several && (
        <div role="menu" aria-label="Repo" className={`${card} absolute left-0 top-full z-10 mt-1 flex min-w-40 flex-col p-0.5`}>
          {repos!.map((r) => (
            <button
              key={r.repo_root}
              type="button"
              role="menuitem"
              className={`rounded-sm px-s2 py-0.5 text-left text-sm hover:bg-surface-2 ${r.repo_root === repo ? "font-medium" : ""}`}
              onClick={() => {
                setOpen(false);
                onRepo(r.repo_root);
              }}
            >
              {r.name}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function SettingsScreen({ client, installCli, appUpdater, appUpdates, releaseBuildUpdater, readAbout, section, ask, rule, repo: routed, current }: SettingsScreenProps) {
  const store = useContext(StoreContext);
  if (!store) throw new Error("SettingsScreen needs a <StoreContext.Provider>");
  const connection = useAppState((state) => state.connection);
  const updateStatus = useAppState((state) => state.appUpdate);
  const pick = useRepo();
  const { repos, setRepo } = pick;
  /* The repo a route names is the one on screen from the first paint, before the repos are known. */
  const repo = pick.repo ?? routed ?? current ?? null;
  const [view, setView] = useState<"you" | "repo">(routed ? "repo" : "you");
  /* Start on the repo the route names, else the current session's; only a route opens its view. */
  useEffect(() => {
    const first = routed ?? current;
    if (first) setRepo(first);
    if (routed) setView("repo");
  }, [routed, current, setRepo]);
  /* A section asked for (the sidebar's problem line, the Inbox) lives on You's side. */
  useEffect(() => {
    if (section) setView("you");
  }, [section, ask]);
  const [config, setConfig] = useState<Config | null>(null);
  const [checks, setChecks] = useState<DoctorCheck[] | null>(null);
  const [doctorError, setDoctorError] = useState<string | null>(null);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const seq = useRef(0);
  /* Staff alone get the Staff only section and its nav button (GET /debug's `staff`). */
  const [staff, setStaff] = useState(false);
  useEffect(() => {
    let live = true;
    client.debug().then(
      (s) => {
        if (live) setStaff(s.staff);
      },
      () => {},
    );
    return () => {
      live = false;
    };
  }, [client]);

  const loadConfig = useCallback(() => {
    const mine = ++seq.current;
    return client.config(repo ?? undefined).then(
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

  const doctor = useCallback(() => {
    setChecking(true);
    // A doctor that didn't answer is a failure to show, never a clean bill.
    return client
      .doctor(repo ?? undefined)
      .then(
        (found) => {
          setChecks(found);
          setDoctorError(null);
        },
        (e: unknown) => {
          setChecks(null);
          setDoctorError(message(e));
        },
      )
      .finally(() => setChecking(false));
  }, [client, repo]);

  const loadMeta = useCallback(() => client.meta().then(setMeta, () => setMeta(null)), [client]);

  // Once the repos are known: the config and the doctor are asked for the repo on screen.
  const known = repos !== null;
  useEffect(() => {
    setConfig(null);
    if (known) void loadConfig();
  }, [loadConfig, known]);
  useEffect(() => {
    setChecks(null);
    if (known) void doctor();
  }, [doctor, known]);
  useEffect(() => {
    void loadMeta();
  }, [loadMeta]);

  /* The person's own settings (~/.brindle/config.json) as a second draft, for every project. */
  const userLoaded = useMemo(() => draftOf(config?.user ?? {}), [config]);
  const [userBase, setUserBase] = useState<Draft>(userLoaded);
  const [userDraft, setUserDraft] = useState<Draft>(userLoaded);
  if (config && userBase !== userLoaded) {
    setUserBase(userLoaded);
    setUserDraft(carryEdits(userBase, userDraft, userLoaded));
  }
  const [saving, setSaving] = useState(false);
  const [userSaved, setUserSaved] = useState<string | null>(null);
  const [userError, setUserError] = useState<string | null>(null);
  const [more, setMore] = useState(false);
  const [yours, setYours] = useState(false);
  const [optional, setOptional] = useState(false);
  /* Engine & health's Details: null follows the health (open when bad), a click decides. */
  const [details, setDetails] = useState<boolean | null>(null);
  const userDirty = changed(userLoaded, userDraft);
  const userMissing = invalid(userDraft);
  const editUser = (key: string, v: unknown) => {
    setUserSaved(null);
    setUserDraft((d) => ({ ...d, [key]: v }));
  };
  /* Your settings: saved to the person's own file, so they apply to every project. */
  const saveUser = async () => {
    if (userDirty.length === 0 || saving || userMissing.length > 0) return;
    setSaving(true);
    setUserError(null);
    try {
      const ack = await client.patchConfig({ scope: "user", values: values(userLoaded, userDraft) });
      setUserSaved(`Saved ${ack.updated.join(", ")} for every project to ${ack.path}`);
      await loadConfig();
    } catch (e) {
      setUserError(message(e));
    } finally {
      setSaving(false);
    }
  };

  const [cliState, setCliState] = useState<"idle" | "installing" | "installed" | "failed">("idle");
  const [cliError, setCliError] = useState<string | null>(null);
  /* The link the shell made, to say where: the row's own line, never a popup. */
  const [cliLink, setCliLink] = useState<string | null>(null);
  /* The engine's update: its job followed on the bus; done, the engine and the doctor are read again. */
  const upgrade = useEngineUpgrade(client, store);
  const upgraded = upgrade.state.kind === "done";
  useEffect(() => {
    if (upgraded) {
      void doctor();
      void loadMeta();
    }
  }, [upgraded, doctor, loadMeta]);
  const updating = upgrade.state.kind === "starting" || upgrade.state.kind === "running";
  const update = () => void upgrade.start();
  const fix = (f: Fix) => (f.kind === "update" ? update() : undefined);
  /* The provider rows, and the installs started from them (the app's: store/providers). */
  const providers = useProviders();
  /* A sign-in ended: the rows read again say whether the provider is signed in now. */
  const signedIn = (provider: string) => async () => (await providers.reload())?.find((r) => r.provider === provider)?.signed_in === true;
  // The page shows the providers as they are now, not as of the last minute's look.
  const { reload: reloadProviders } = providers;
  useEffect(() => {
    void reloadProviders();
  }, [reloadProviders]);
  /* How each provider connects (GET /providers/{p}/connection), read with the rows and after a change. */
  const [connections, setConnections] = useState<Record<string, ProviderConnection>>({});
  /* Per provider, the newest read asked for: an older one answering late is dropped. */
  const asked = useRef<Record<string, number>>({});
  const loadConnection = useCallback(
    (provider: string) => {
      const mine = (asked.current[provider] = (asked.current[provider] ?? 0) + 1);
      return client.providerConnection(provider, repo ?? undefined).then(
        (c) => asked.current[provider] === mine && setConnections((all) => ({ ...all, [provider]: c })),
        () => undefined,
      );
    },
    [client, repo],
  );
  // Read again with every look at the providers: a sign-in, sign-out, install or turn-on changes whether each is ready.
  const rows = providers.rows;
  useEffect(() => {
    for (const r of rows ?? []) void loadConnection(r.provider);
  }, [rows, loadConnection]);
  /* A key no provider reads being set or replaced (its name). */
  const [otherEditing, setOtherEditing] = useState<string | null>(null);
  const [otherError, setOtherError] = useState<string | null>(null);
  const removeOther = async (name: string) => {
    setOtherError(null);
    try {
      await client.deleteKey(name);
      await loadConfig();
    } catch (e) {
      setOtherError(`Couldn't remove ${name}: ${message(e)}`);
    }
  };
  /* A key the shell has and brindle doesn't, imported on request (POST /keys/{name}/import). */
  const [importError, setImportError] = useState<string | null>(null);
  const importShellKey = async (name: string) => {
    setImportError(null);
    try {
      await client.importKey(name, repo ?? undefined);
      await loadConfig();
      // The key may make its provider ready to launch.
      for (const r of rows ?? []) void loadConnection(r.provider);
    } catch (e) {
      setImportError(`Couldn't import ${name}: ${message(e)}`);
    }
  };
  // Opened from the sidebar's problem line: scrolled to Providers, again once the sections above it have loaded.
  const placed = config !== null;
  useEffect(() => {
    if (section) document.getElementById(section)?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }, [section, ask, placed, view]);
  const install = async () => {
    if (!installCli) return;
    setCliState("installing");
    setCliError(null);
    try {
      const link = await installCli();
      setCliLink(link && typeof link === "object" && typeof link.path === "string" ? link.path : null);
      setCliState("installed");
    } catch (e) {
      setCliState("failed");
      setCliError(message(e));
    }
  };

  const health = checks?.filter((c) => !isOptional(c) && !inSignInRows(c)) ?? [];
  const optionals = checks?.filter((c) => isOptional(c) && !inSignInRows(c)) ?? [];
  // What needs attention here: the sign-in rows show their own state in their own section.
  const wrong = health.filter((c) => c.level !== "ok" || c.name === "brindle update").length;
  const upgradeNotice = checks ? updateNotice(checks) : undefined;
  // Details: closed unless health is bad (a failed check or a doctor that didn't answer), until the user chooses.
  const healthBad = !!doctorError || health.some((c) => c.level === "fail");
  const detailsOpen = details ?? healthBad;
  const common = YOURS.filter((s) => s.common);
  const others = YOURS.filter((s) => !s.common);
  const shown = SETTINGS.map((s) => s.key);
  // What the person set for every repo that has no row here.
  const yourLines = userLines(config?.user ?? {}).filter(([k]) => !shown.includes(k));
  const yourRow = (s: Setting) => <SettingRow key={s.key} s={s} value={userDraft[s.key]} dirty={userDirty.includes(s.key)} disabled={saving} onChange={(v) => editUser(s.key, v)} />;
  const repoView = view === "repo" && !!repo;

  return (
    <section className="flex min-h-0 flex-col gap-s3" aria-label="Settings">
      <PageHeader title="Settings" lede={repoView ? "This repo's settings: what the team sets, what is just for you, and what follows your own." : "Your settings apply to every project; a project can override them. Below: permissions, sign-ins and engine health."}>
        <ScopeSwitch view={repoView ? "repo" : "you"} onView={setView} repo={repo} repos={repos} onRepo={setRepo} disabled={saving} />
      </PageHeader>
      {repoView && (
        <section className="flex min-h-0 flex-col gap-s3" aria-label="Project settings" data-testid="project-settings">
          <RepoSettings key={repo} client={client} repo={repo} />
        </section>
      )}
      {!repoView && <SectionNav staff={staff} />}
      {!repoView && (
      <div className="border-t border-line">
        <Row label="Appearance">
          <ThemeToggle choices />
        </Row>
      </div>
      )}
      {!repoView && repos && repos.length === 0 && <p className="text-ink-3">{NO_REPO}</p>}
      {!repoView && <>
      {loadError && (
        <div role="alert" className={`${alert} flex items-center justify-between gap-s2`}>
          <span>Could not load settings: {loadError}</span>
          <button type="button" className={button} onClick={() => void loadConfig()}>
            Retry
          </button>
        </div>
      )}
      {config && (
        <Section id="your-settings" title="Your settings" blurb="Saved to ~/.brindle/config.json and applied to every project, unless a repo sets one itself." testId="your-settings">
          {common.map(yourRow)}
          {more && others.map(yourRow)}
          <div className="flex flex-col gap-1 px-s2 py-1">
            <div className="flex flex-wrap items-center gap-s2">
              <More open={more} onToggle={() => setMore((m) => !m)} count={others.length} />
              {yourLines.length > 0 && <More open={yours} onToggle={() => setYours((y) => !y)} label="Other settings you have" count={yourLines.length} />}
            </div>
            {yours && <RestOfConfig lines={yourLines} testId="user-rest" note="~/.brindle/config.json: what you have set for every repo, which a repo's own files override." />}
          </div>
          {(userDirty.length > 0 || userSaved || userError) && (
            <div className="flex flex-col gap-s1 px-s2 py-s1" data-testid="user-save">
              {userDirty.length > 0 && (
                <div className="flex flex-wrap items-center gap-s2">
                  <span className="text-sm font-medium">{userDirty.length === 1 ? "1 change" : `${userDirty.length} changes`}, for every project</span>
                  <span className="flex-1" />
                  <button type="button" className={quiet} disabled={saving} onClick={() => setUserDraft(userLoaded)}>
                    Discard
                  </button>
                  <button type="button" className={button} disabled={saving || userMissing.length > 0} onClick={() => void saveUser()}>
                    {saving ? "Saving…" : "Save your settings"}
                  </button>
                </div>
              )}
              {userMissing.length > 0 && userDirty.length > 0 && (
                <p className={alert} data-testid="settings-invalid">
                  {userMissing.map((k) => SETTINGS.find((s) => s.key === k)?.label ?? k).join(", ")} needs a number: the engine keeps what is saved as it is, and an empty value would break it.
                </p>
              )}
              {userSaved && userDirty.length === 0 && (
                <p role="status" className={notice}>
                  {userSaved}
                </p>
              )}
              {userError && (
                <p role="alert" className={alert}>
                  Could not save: {userError}
                </p>
              )}
            </div>
          )}
        </Section>
      )}

      {config && (
        <Section id="you" title="How Claude asks you" blurb="Whether Claude's held requests wait for you in the Inbox or Claude decides most itself." testId="user-settings">
          <PermissionMode client={client} />
        </Section>
      )}

      <Permissions client={client} rule={rule} ask={ask} />

      <Section id="organization" title="Organization" blurb="Your organization's members, their roles, and the approval limits it sets." testId="org-approvals">
        <Row label="Organization approvals" help="For an organization's admins: assign roles and set which approvals members may save." wide>
          <OrgApprovals client={client} />
        </Row>
      </Section>

      <Section
        id="providers"
        title="Providers"
        blurb="How brindle reaches each model. Each one can sign in, use an API key, or go through your cloud."
        testId="providers"
      >
        {providers.rows === null ? (
          <div className="flex items-center gap-s2 px-s2 py-s1 text-sm text-ink-3" data-testid="providers-loading">
            {providers.error ? (
              <>
                <span className="text-sap">Couldn't look for the providers: {plain(providers.error)}</span>
                <button type="button" className={quiet} onClick={() => void providers.reload()}>
                  Try again
                </button>
              </>
            ) : (
              "Looking for the providers…"
            )}
          </div>
        ) : (
          <>
            {/* The rows kept from the last look, and why the newest look failed. */}
            {providers.error && (
              <div className="flex flex-wrap items-center gap-s2 px-s2 py-s1 text-sm" role="alert" data-testid="providers-refresh-error">
                <span className="text-sap">Couldn't look for the providers again: {plain(providers.error)}</span>
                <button type="button" className={quiet} onClick={() => void providers.reload()}>
                  Try again
                </button>
              </div>
            )}
            {providers.rows.map((r) => (
              <ProviderRowView
                key={r.provider}
                client={client}
                repo={repo}
                connection={connections[r.provider] ?? null}
                keys={config?.keys ?? null}
                onChanged={() => {
                  void loadConnection(r.provider);
                  void loadConfig();
                }}
                row={r}
                install={providers.installs[r.provider]}
                detectError={providers.error}
                check={signedIn(r.provider)}
                onInstall={() => void providers.install(r.provider)}
                onTurnOn={(path) => providers.turnOn(r.provider, path)}
                onDismiss={() => providers.dismiss(r.provider)}
                onRemove={() => providers.remove(r.provider)}
                onSignOut={() => providers.signOut(r.provider)}
                onLookAgain={() => void providers.reload()}
              />
            ))}
          </>
        )}
        {/* A key the shell exports and brindle doesn't keep yet: imported on request, never on its own. */}
        {config?.keys
          .filter((k) => k.importable)
          .map((k) => (
            <div key={k.name} className="flex flex-wrap items-center gap-s2 px-s2 py-s1 text-sm text-ink-2" data-testid={`key-import-${k.name}`}>
              <span>
                Found <span className={mono}>{k.name}</span> in your shell, not saved in brindle.
              </span>
              <button type="button" className={button} onClick={() => void importShellKey(k.name)}>
                Import
              </button>
            </div>
          ))}
        {importError && (
          <p className={`${alert} mx-s2 my-s1`} role="alert">
            {importError}
          </p>
        )}
        {/* Keys a profile names (api_key_env) that no provider reads: stored, replaced and removed here. */}
        {config && config.keys.some(otherKey) && (
          <Row label="Other keys" help="Keys your profiles name that no provider reads. Stored in your keychain." wide testId="other-keys">
            <ul className="flex w-full flex-col divide-y divide-line">
              {config.keys.filter(otherKey).map((k) => (
                <li key={k.name} className="flex flex-col gap-s1 py-1" data-testid={`other-key-${k.name}`} data-set={k.set}>
                  <span className="flex flex-wrap items-center gap-s1 text-sm">
                    <span className={mono}>{k.name}</span>
                    <span className="text-xs text-ink-3">{k.set ? <span className={mono}>••••{k.last4}</span> : "not set"}</span>
                    <span className="flex-1" />
                    <button type="button" className={quiet} onClick={() => setOtherEditing(otherEditing === k.name ? null : k.name)}>
                      {k.set ? "Replace" : "Set"}
                    </button>
                    {k.set && (
                      <button type="button" className={quiet} onClick={() => void removeOther(k.name)}>
                        Remove
                      </button>
                    )}
                  </span>
                  {otherEditing === k.name && (
                    <ApiKeyPaste
                      client={client}
                      provider={null}
                      expect={k.name}
                      keys={config.keys}
                      repo={repo}
                      onSaved={() => {
                        setOtherEditing(null);
                        void loadConfig();
                      }}
                      onCancel={() => setOtherEditing(null)}
                    />
                  )}
                </li>
              ))}
            </ul>
            {otherError && (
              <p className={alert} role="alert">
                {otherError}
              </p>
            )}
          </Row>
        )}
      </Section>

      <Section
        id="engine"
        title="Engine & health"
        blurb={doctorError ? "The checks didn't run." : checks === null ? "Checking…" : wrong === 0 ? "Everything brindle needs is here." : wrong === 1 ? "1 thing needs attention." : `${wrong} things need attention.`}
        actions={
          <button type="button" className={quiet} disabled={checking} onClick={() => void doctor()}>
            {checking ? "Checking…" : doctorError ? "Try again" : "Check again"}
          </button>
        }
        testId="engine"
      >
        <div className="flex flex-col gap-s1 px-s2 py-s1" data-testid="engine-health-summary">
          <div className="flex flex-wrap items-center gap-x-s2 gap-y-1 text-xs text-ink-2">
            {meta ? <span className={mono}>brindle {meta.engine_version}</span> : <span>No engine</span>}
            <span data-testid="health-status" data-bad={healthBad ? "true" : "false"} className={healthBad ? "text-sap" : undefined}>
              {doctorError ? "Checks didn't run" : checks === null ? "Checking…" : wrong === 0 ? "Healthy" : wrong === 1 ? "1 needs attention" : `${wrong} need attention`}
            </span>
            {cliState !== "idle" && (
              <span data-testid="cli-status">{cliState === "installing" ? "Installing CLI…" : cliState === "installed" ? "CLI installed" : "CLI install failed"}</span>
            )}
            <span className="flex-1" />
            {upgradeNotice && upgrade.state.kind === "idle" && (
              <button type="button" className={main} onClick={update}>
                Update brindle
              </button>
            )}
            {upgrade.state.kind !== "idle" && (
              <span data-testid="update-status">{upgrade.state.kind === "done" ? "Updated" : upgrade.state.kind === "failed" ? "Update failed: see Details" : "Updating…"}</span>
            )}
            <More open={detailsOpen} onToggle={() => setDetails(!detailsOpen)} label="Details" />
          </div>
        </div>
        {detailsOpen && (
          <div data-testid="engine-details">
            {upgrade.state.kind !== "idle" && (
              <div className="px-s2 py-1">
                <UpgradeCard state={upgrade.state} onDismiss={upgrade.dismiss} />
              </div>
            )}
        <Row label="Daemon connection" help="The live connection between the app and brindle." testId="daemon-connection">
          <span className="text-xs text-ink-2" data-testid="connection">daemon · {connection === "open" ? "connected" : connection === "closed" ? "reconnecting…" : connection === "unauthorized" ? "token refused" : connection === "connecting" ? "connecting…" : "no engine"}</span>
        </Row>
        <Row
          label="Engine"
          help={
            upgradeNotice ? (
              <span className="text-sap" data-testid="update-notice">
                {plain(upgradeNotice)}
              </span>
            ) : upgradeNotice === undefined ? (
              meta ? "Whether a newer brindle is out comes from the health check, which hasn't answered." : "Not connected."
            ) : (
              "Up to date, as far as the last check against PyPI went."
            )
          }
          testId="engine-row"
        >
          {meta && (
            <span className={`${mono} text-xs text-ink-2`}>
              brindle {meta.engine_version} · API {meta.api_version}
              {meta.airgap ? " · air-gapped" : ""}
            </span>
          )}
        </Row>
        <Row
          label="Command-line tool"
          help={cliState === "installed" ? `Installed${cliLink ? ` as ${cliLink}` : ""}: a terminal can run brindle now.` : cliError ? `Could not install: ${cliError}` : installCli ? "Puts the brindle command on your PATH, so a terminal can run it." : "Puts the brindle command on your PATH. Not in this build yet: it comes with the bundled engine."}
          testId="install-cli"
        >
          <button type="button" className={button} disabled={!installCli || cliState === "installing" || cliState === "installed"} title={installCli ? undefined : "Not in this build yet"} onClick={() => void install()}>
            {cliState === "installing" ? "Installing…" : cliState === "installed" ? "Installed" : "Install command-line tool"}
          </button>
        </Row>
        {doctorError && (
          <p role="alert" className={`${alert} m-s2`} data-testid="doctor-error">
            Could not run the checks: {doctorError}
          </p>
        )}
        {checks && (
          <ul className="flex flex-col py-1" data-testid="doctor">
            {health.map((c) => (
              <HealthRow key={`${c.name}:${c.detail}`} c={c} busy={updating} onFix={fix} />
            ))}
          </ul>
        )}
        {optionals.length > 0 && (
          <div className="flex flex-col gap-1 px-s2 py-1">
            <More open={optional} onToggle={() => setOptional((o) => !o)} label="Only some setups need these" count={optionals.length} />
            {optional && (
              <ul className="-mx-s2 flex flex-col" data-testid="doctor-optional">
                {optionals.map((c) => (
                  <HealthRow key={`${c.name}:${c.detail}`} c={c} busy={updating} onFix={fix} />
                ))}
              </ul>
            )}
          </div>
        )}
          </div>
        )}
      </Section>

      {staff && (
        <Section id="staff" title="Staff only" blurb="Internal tools for Brindle staff. Not shown to customers." staff testId="staff-only">
          <DebugLogging client={client} />
          <AppUpdate client={client} updater={appUpdater} release={releaseBuildUpdater} readAbout={readAbout} />
        </Section>
      )}

      <About read={readAbout} updates={appUpdates} status={updateStatus} />
      </>}
    </section>
  );
}
