/* Settings: this machine's brindle, in six sections (About last) with a small nav at
   the top: This repo (the handful of settings people change most, the rest
   behind More), You (how Claude's permission requests are handled, your
   defaults for every repo), Permissions (the permission policy: Permissions.tsx), Providers (one row per
   provider CLI and per key, each with its state and its buttons) and
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

   The repo's settings (GET /v1/config) are edited as a draft and saved with
   PATCH /v1/config to this repo's own config.local.json or to the person's
   config for every repo, from a save bar at the foot. The Claude
   permission mode is the person's own (GET and PATCH /v1/user-settings) and
   saves as soon as it is picked. Keys show as names and last four characters
   only; setting one is typed into a password field and stored write-only
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
   /v1/debug's `staff`) get a fifth section at the foot, Debug logging: one
   switch (PUT /v1/debug) for uploading debug logs to brindle (api/debug.ts).
   In the app staff also get App update: one Update button that checks this
   build's feed (a release candidate's rc channel) and installs what it
   finds, then offers a restart. It is how a release candidate updates at
   all: an rc build never checks by itself (src-tauri/src/updates.rs).
   Beside it, staff can install the release branch's latest build, before
   it ships, and restart into it (ReleaseBuildUpdate).
   Last, About: the app's version and its license files (About.tsx). */

import { useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { ClaudePermissionMode, Config, DaemonClient, DebugState, DoctorCheck, Meta, ProviderInstalled, UserSettings } from "../../api/client";
import { debugState } from "../../api/debug";
import { connected, factsOf, settingsNote, turnedOff } from "../../api/providers";
import type { Install } from "../../api/providersInstall";
import type { About as AboutInfo, AppUpdater, CliLink, ReleaseBuildUpdater } from "../../api/tauri";
import { plain } from "../../api/wording";
import { UpgradeCard, useEngineUpgrade } from "../../components/EngineUpgrade";
import { PageHeader } from "../../components/PageHeader";
import { ProgressCard } from "../../components/ProgressCard";
import { ProviderSignIn } from "../../components/ProviderSignIn";
import { About } from "./About";
import { OrgApprovals } from "./OrgApprovals";
import { Permissions } from "./Permissions";
import { ReleaseBuildUpdate } from "./ReleaseBuildUpdate";
import { NO_REPO, RepoSelect, useRepo } from "../../components/RepoSelect";
import { alert, button, card, field, h2, label, mono, notice, pill, primary, quiet } from "../../components/ui";
import { useProviders } from "../../store/providers";
import { StoreContext } from "../../store/useStore";
import {
  MARK,
  SCOPES,
  SETTINGS,
  changed,
  draftOf,
  fixFor,
  inSignInRows,
  invalid,
  isOptional,
  listOf,
  listText,
  repoOnlyChanged,
  updateNotice,
  userLines,
  values,
  type Draft,
  type Fix,
  type Scope,
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
  /* The section to scroll to on opening: "providers" from the sidebar's problem line, "permissions" from the Inbox. */
  section?: "providers" | "permissions";
  /* A new value asks to scroll to `section` again, the page already open. */
  ask?: number;
  /* A permission rule to open in its editor (the Inbox's "Open the deny rule"). */
  rule?: string;
}

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

const SECTIONS = [
  { id: "this-repo", label: "This repo" },
  { id: "you", label: "You" },
  { id: "permissions", label: "Permissions" },
  { id: "providers", label: "Providers" },
  { id: "engine", label: "Engine & health" },
  { id: "about", label: "About" },
];

/* The small in-page nav: one button per section, scrolling to it. */
function SectionNav() {
  return (
    <nav aria-label="Settings sections" className="flex flex-wrap gap-1">
      {SECTIONS.map((s) => (
        <button
          key={s.id}
          type="button"
          className={`${quiet} rounded-full border border-line px-s2 text-xs`}
          onClick={() => document.getElementById(s.id)?.scrollIntoView?.({ behavior: "smooth", block: "start" })}
        >
          {s.label}
        </button>
      ))}
    </nav>
  );
}

/* A section: its heading and one-line blurb, actions on the right, and a card of rows. */
function Section({ id, title, blurb, actions, children, testId }: { id: string; title: string; blurb?: ReactNode; actions?: ReactNode; children: ReactNode; testId: string }) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="flex scroll-mt-s2 flex-col gap-s1" data-testid={testId}>
      <header className="flex flex-wrap items-baseline gap-x-s2 gap-y-0.5">
        <h2 id={`${id}-title`} className={h2}>
          {title}
        </h2>
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

const Edited = () => <span className={`${pill} bg-branch-soft text-branch`}>edited</span>;

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
      className={`relative h-5 w-9 shrink-0 rounded-full border transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${checked ? "border-branch bg-branch" : "border-line-strong bg-surface-2"}`}
      onClick={() => onChange(!checked)}
    >
      <span aria-hidden className={`absolute top-0.5 size-3.5 rounded-full transition-[left] ${checked ? "left-[18px] bg-on-branch" : "left-0.5 bg-ink-3"}`} />
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

function SettingRow({ s, value, dirty, disabled, onChange }: { s: Setting; value: unknown; dirty: boolean; disabled: boolean; onChange(value: unknown): void }) {
  return (
    <Row
      label={s.label}
      help={s.blurb}
      htmlFor={`setting-${s.key}`}
      wide={s.kind === "list"}
      badges={
        <>
          {s.pro && <span className={`${pill} bg-fawn-soft text-fawn`}>Pro</span>}
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
function More({ open, onToggle, label: text = "More", count }: { open: boolean; onToggle(): void; label?: string; count?: number }) {
  return (
    <button type="button" className={`${quiet} self-start text-xs`} aria-expanded={open} onClick={onToggle}>
      {open ? "Less" : text}
      {count !== undefined && !open ? ` (${count})` : ""} {open ? "▴" : "▾"}
    </button>
  );
}

/* The settings from the config the screen doesn't edit, as key: value lines. */
function RestOfConfig({ lines, testId, note }: { lines: [string, string][]; testId: string; note: string }) {
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
        <div role="radiogroup" aria-label="Claude permission mode" className="inline-flex rounded-md border border-line-strong bg-surface p-0.5">
          {MODES.map((m) => (
            <button
              key={m.value}
              type="button"
              role="radio"
              aria-checked={current === m.value}
              disabled={saving || !settings}
              title={m.blurb}
              className={`rounded-sm px-s2 py-0.5 text-sm disabled:opacity-50 ${current === m.value ? "bg-branch text-on-branch" : "text-ink-2 hover:bg-surface-2"}`}
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
    <Section id="debug" title="Debug logging" testId="debug-logging">
      <Row label="Upload debug logs" htmlFor="debug-logging" help="While this is on, brindle's and this app's debug logs upload to brindle." testId="debug-row">
        <Toggle id="debug-logging" label="Debug logging" checked={state.enabled} disabled={saving} onChange={(on) => void turn(on)} />
      </Row>
      {error && (
        <p role="alert" className={`${alert} m-s2`}>
          Could not save: {error}
        </p>
      )}
    </Section>
  );
}

/* Staff-only app update (owner decision): a release candidate never updates
   by itself, so staff update it here, and can install the release branch's
   latest build (ReleaseBuildUpdate). Shown only when GET /debug says this
   account is staff and the shell gives an updater; anyone else sees nothing.
   The shell checks staff again before installing. */
function AppUpdate({ client, updater, release }: { client: DaemonClient; updater?: AppUpdater; release?: ReleaseBuildUpdater }) {
  const [staff, setStaff] = useState(false);
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
    <Section id="app-update" title="App update" testId="app-update">
      {updater && (
        <Row label="Update the app" help={help} testId="app-update-row">
          {state === "installed" || state === "restarting" ? (
            <button type="button" className={primary} disabled={state === "restarting"} onClick={() => void restart(updater)}>
              Restart
            </button>
          ) : (
            <button type="button" className={button} disabled={state === "checking"} onClick={() => void update(updater)}>
              {state === "checking" ? "Updating…" : "Update"}
            </button>
          )}
        </Row>
      )}
      {release && <ReleaseBuildUpdate updater={release} />}
      {error && (
        <p role="alert" className={`${alert} m-s2`}>
          Could not update: {error}
        </p>
      )}
    </Section>
  );
}

/* One key's value, typed into a password field and sent to PUT /keys/{name}
   (write-only: the engine stores it as `brindle keys set` does and answers
   the last four). The value lives in this field until Save and nowhere
   after: not in the app's state, not in its logs. */
function KeyField({ client, name, repo, onStored, onCancel }: { client: DaemonClient; name: string; repo: string | null; onStored(): void; onCancel(): void }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    if (!value.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await client.putKey(name, value, repo ?? undefined);
      setValue("");
      onStored();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form
      className="flex w-full flex-wrap items-center gap-s1"
      aria-label={`Value for ${name}`}
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <input
        className={`${field} min-w-64 flex-1 font-mono`}
        type="password"
        autoComplete="off"
        aria-label={`${name} value`}
        placeholder={`Paste the ${name} value`}
        value={value}
        disabled={busy}
        autoFocus
        onChange={(e) => setValue(e.target.value)}
      />
      <button type="submit" className={primary} disabled={busy || !value.trim()}>
        {busy ? "Saving…" : "Save"}
      </button>
      <button type="button" className={quiet} disabled={busy} onClick={onCancel}>
        Cancel
      </button>
      {error && (
        <p role="alert" className={`${alert} w-full`}>
          Could not store {name}: {error}
        </p>
      )}
    </form>
  );
}

type RowState = "signed_in" | "signed_out" | "unknown" | "not_installed" | "off";
const DOT: Record<RowState, string> = { signed_in: "bg-pass", signed_out: "bg-fawn", unknown: "bg-ink-3", not_installed: "bg-ink-3", off: "bg-ink-3" };
const STATE_TEXT: Record<RowState, string> = { signed_in: "Signed in", signed_out: "Not signed in", unknown: "Sign-in unknown", not_installed: "Not installed", off: "Removed from brindle" };

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
function ProviderRowView({ row, install, detectError, check, onInstall, onTurnOn, onDismiss, onRemove, onLookAgain }: {
  row: ProviderInstalled;
  install: Install | undefined;
  /* Why the rows couldn't be read again (after a finished install, its row is stale until they are). */
  detectError: string | null;
  check(): Promise<boolean>;
  onInstall(): void;
  onTurnOn(path: string): Promise<void>;
  onDismiss(): void;
  onRemove(): Promise<void>;
  onLookAgain(): void;
}) {
  const f = factsOf(row);
  const state = rowState(row);
  const on = connected(row);
  const [signing, setSigning] = useState(false);
  const [busy, setBusy] = useState<"removing" | "connecting" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const installing = install?.status === "starting" || install?.status === "running";
  const act = async (kind: "removing" | "connecting", run: () => Promise<void>, what: string) => {
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
  // An off provider still found comes back as it is; one missing runs the maker's installer.
  const connectNow = () => (turnedOff(row) && row.path ? void act("connecting", () => onTurnOn(row.path!), "connect") : onInstall());
  const help = (
    <>
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
      {actionError && (
        <span className="block text-sap" role="alert">
          {actionError}
        </span>
      )}
    </>
  );
  return (
    <Row label={f.name} help={help} wide={signing || !!install} testId={`provider-${row.provider}`} data={{ provider: row.provider, state, install: install?.status }}>
      {!signing && (
        <span className="flex items-center gap-1 text-xs text-ink-2" data-testid="provider-state">
          <span className={`size-2 rounded-full ${DOT[state]}`} aria-hidden />
          {STATE_TEXT[state]}
        </span>
      )}
      {!on && !install && (
        <button type="button" className={`${primary} px-s1 py-0.5 text-xs`} aria-label={`Connect ${f.name}`} disabled={busy !== null} onClick={connectNow}>
          {busy === "connecting" ? "Connecting…" : "Connect"}
        </button>
      )}
      {on && (row.signed_in !== true || signing) && <ProviderSignIn provider={row.provider} check={check} small onActive={setSigning} details={false} />}
      {on && !signing && (
        <button type="button" className={quiet} aria-label={`Remove ${f.name} from brindle`} disabled={busy !== null} onClick={() => void remove()}>
          {busy === "removing" ? "Removing…" : "Remove from brindle"}
        </button>
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

const TONE: Record<string, string> = { ok: "text-pass", warn: "text-fawn", fail: "text-sap" };

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

export function SettingsScreen({ client, installCli, appUpdater, releaseBuildUpdater, readAbout, section, ask, rule }: SettingsScreenProps) {
  const store = useContext(StoreContext);
  if (!store) throw new Error("SettingsScreen needs a <StoreContext.Provider>");
  const pick = useRepo();
  const { repo, repos } = pick;
  const [config, setConfig] = useState<Config | null>(null);
  const [checks, setChecks] = useState<DoctorCheck[] | null>(null);
  const [doctorError, setDoctorError] = useState<string | null>(null);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const seq = useRef(0);

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

  /* The repo's settings as a draft, saved where the person says. */
  const loaded = useMemo(() => draftOf(config?.repo ?? {}), [config]);
  const [base, setBase] = useState<Draft>(loaded);
  const [draft, setDraft] = useState<Draft>(loaded);
  // A config read anew (a reload, another repo) replaces the draft in the same render.
  if (base !== loaded) {
    setBase(loaded);
    setDraft(loaded);
  }
  const [scope, setScope] = useState<Scope>("repo");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [more, setMore] = useState(false);
  const [rest, setRest] = useState(false);
  const [yours, setYours] = useState(false);
  const [optional, setOptional] = useState(false);
  useEffect(() => {
    setSaved(null);
    setSaveError(null);
  }, [repo]);
  const dirty = changed(loaded, draft);
  const missing = invalid(draft);
  // A setting the person's own file can't carry (checks) pins the save to the repo.
  const repoOnly = repoOnlyChanged(loaded, draft);
  const pinned = repoOnly.length > 0;
  const target: Scope = pinned ? "repo" : scope;
  const edit = (key: string, v: unknown) => {
    setSaved(null);
    setDraft((d) => ({ ...d, [key]: v }));
  };
  const save = async () => {
    if (!repo || dirty.length === 0 || saving || missing.length > 0) return;
    setSaving(true);
    setSaveError(null);
    try {
      const ack = await client.patchConfig({ scope: target, repo: target === "repo" ? repo : undefined, values: values(loaded, draft) });
      setSaved(`Saved ${ack.updated.join(", ")} to ${ack.path}`);
      await loadConfig();
    } catch (e) {
      setSaveError(message(e));
    } finally {
      setSaving(false);
    }
  };

  /* A key being set (its name), by name from the list or typed; stored, the config is read again. */
  const [editing, setEditing] = useState<string | null>(null);
  const [stored, setStored] = useState<string | null>(null);
  const [keyName, setKeyName] = useState("");
  const [cliState, setCliState] = useState<"idle" | "installing" | "installed" | "failed">("idle");
  const [cliError, setCliError] = useState<string | null>(null);
  /* The link the shell made, to say where: the row's own line, never a popup. */
  const [cliLink, setCliLink] = useState<string | null>(null);
  const keyStored = (name: string) => {
    setEditing(null);
    setStored(name);
    void loadConfig();
  };
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
  // Opened from the sidebar's problem line: scrolled to Providers, again once the sections above it have loaded.
  const placed = config !== null;
  useEffect(() => {
    if (section) document.getElementById(section)?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }, [section, ask, placed]);
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

  const validKey = /^[A-Z][A-Z0-9_]{2,}$/.test(keyName.trim());
  const health = checks?.filter((c) => !isOptional(c) && !inSignInRows(c)) ?? [];
  const optionals = checks?.filter((c) => isOptional(c) && !inSignInRows(c)) ?? [];
  // What needs attention here: the sign-in rows show their own state in their own section.
  const wrong = health.filter((c) => c.level !== "ok" || c.name === "brindle update").length;
  const upgradeNotice = checks ? updateNotice(checks) : undefined;
  const common = SETTINGS.filter((s) => s.common);
  const others = SETTINGS.filter((s) => !s.common);
  const shown = SETTINGS.map((s) => s.key);
  const restLines: [string, string][] = Object.entries(config?.repo ?? {})
    .filter(([k]) => !shown.includes(k))
    .map(([k, v]) => [k, typeof v === "string" ? v : JSON.stringify(v)]);
  const yourLines = userLines(config?.user ?? {});

  return (
    // Room at the foot for the save bar, which sticks there while there are changes.
    <section className={`flex min-h-0 flex-col gap-s3 ${dirty.length > 0 ? "pb-s12" : ""}`} aria-label="Settings">
      <PageHeader title="Settings" lede="This repo's settings, your own, the providers and keys agents use, and whether everything brindle needs is here.">
        <RepoSelect {...pick} disabled={saving} />
      </PageHeader>
      <SectionNav />
      {repos && repos.length === 0 && <p className="text-ink-3">{NO_REPO}</p>}
      {loadError && (
        <div role="alert" className={`${alert} flex items-center justify-between gap-s2`}>
          <span>Could not load settings: {loadError}</span>
          <button type="button" className={button} onClick={() => void loadConfig()}>
            Retry
          </button>
        </div>
      )}
      {config && repo && (
        <Section id="this-repo" title="This repo" blurb="As brindle reads it: this repo's own files over your defaults." testId="repo-settings">
          {common.map((s) => (
            <SettingRow key={s.key} s={s} value={draft[s.key]} dirty={dirty.includes(s.key)} disabled={saving} onChange={(v) => edit(s.key, v)} />
          ))}
          {more && others.map((s) => <SettingRow key={s.key} s={s} value={draft[s.key]} dirty={dirty.includes(s.key)} disabled={saving} onChange={(v) => edit(s.key, v)} />)}
          <div className="flex flex-col gap-1 px-s2 py-1">
            <div className="flex flex-wrap items-center gap-s2">
              <More open={more} onToggle={() => setMore((m) => !m)} count={others.length} />
              {restLines.length > 0 && <More open={rest} onToggle={() => setRest((r) => !r)} label="Everything else" count={restLines.length} />}
            </div>
            {rest && <RestOfConfig lines={restLines} testId="config-rest" note="Edit these in .brindle/config.json (the repo's, committed) or .brindle/config.local.json (yours)." />}
          </div>
        </Section>
      )}

      {config && (
        <Section id="you" title="You" blurb="How Claude's requests reach you, and your defaults for every repo." testId="user-settings">
          <PermissionMode client={client} />
          {yourLines.length > 0 && (
            <div className="flex flex-col gap-1 px-s2 py-1">
              <More open={yours} onToggle={() => setYours((y) => !y)} label="Your defaults for every repo" count={yourLines.length} />
              {yours && <RestOfConfig lines={yourLines} testId="user-rest" note="~/.brindle/config.json: what you have set for every repo, which a repo's own files override." />}
            </div>
          )}
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
        blurb="The agent CLIs brindle runs, each as brindle finds it on this Mac and kept up to date by itself, and the keys agents start with. A key is kept in your keychain and shows as its last four characters only."
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
                row={r}
                install={providers.installs[r.provider]}
                detectError={providers.error}
                check={signedIn(r.provider)}
                onInstall={() => void providers.install(r.provider)}
                onTurnOn={(path) => providers.turnOn(r.provider, path)}
                onDismiss={() => providers.dismiss(r.provider)}
                onRemove={() => providers.remove(r.provider)}
                onLookAgain={() => void providers.reload()}
              />
            ))}
          </>
        )}
        {config &&
          config.keys.map((k) => (
            <Row
              key={k.name}
              label={<span className={mono}>{k.name}</span>}
              help={stored === k.name && editing !== k.name ? <span role="status">{k.name} stored.</span> : undefined}
              wide={editing === k.name}
              testId={`key-${k.name}`}
              data={{ key: k.name, set: String(k.set) }}
            >
              {editing === k.name ? (
                <KeyField client={client} name={k.name} repo={repo} onStored={() => keyStored(k.name)} onCancel={() => setEditing(null)} />
              ) : (
                <>
                  {k.set ? (
                    <span className="flex items-center gap-1 text-xs text-ink-2">
                      <span className="size-2 rounded-full bg-pass" aria-hidden />
                      Set <span className={mono}>••••{k.last4}</span>
                    </span>
                  ) : (
                    <span className="flex items-center gap-1 text-xs text-ink-2">
                      <span className="size-2 rounded-full bg-fawn" aria-hidden />
                      Not set
                    </span>
                  )}
                  <button
                    type="button"
                    className={button}
                    aria-label={`${k.set ? "Change" : "Set"} ${k.name}`}
                    onClick={() => {
                      setStored(null);
                      setEditing(k.name);
                    }}
                  >
                    {k.set ? "Change…" : "Set…"}
                  </button>
                </>
              )}
            </Row>
          ))}
        {config && editing !== null && !config.keys.some((k) => k.name === editing) && (
            // Keyed by the name: a field for another new key starts empty, never with the last one's value.
            <Row key={editing} label={<span className={mono}>{editing}</span>} wide testId={`key-${editing}`} data={{ key: editing, set: "false" }}>
              <KeyField key={editing} client={client} name={editing} repo={repo} onStored={() => keyStored(editing)} onCancel={() => setEditing(null)} />
            </Row>
          )}
        {config && (
          <form
            className="flex flex-wrap items-center gap-s1 px-s2 py-s1"
            aria-label="Add a key"
            onSubmit={(e) => {
              e.preventDefault();
              if (!validKey) return;
              setStored(null);
              setEditing(keyName.trim());
              setKeyName("");
            }}
          >
            <input className={`${field} w-56 font-mono`} aria-label="Key name" placeholder="ANOTHER_API_KEY" value={keyName} onChange={(e) => setKeyName(e.target.value.toUpperCase())} />
            <button type="submit" className={quiet} disabled={!validKey}>
              Add a key…
            </button>
          </form>
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
        <Row
          label="Engine"
          wide={upgrade.state.kind !== "idle"}
          help={
            upgradeNotice ? (
              <span className="text-fawn" data-testid="update-notice">
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
          {upgradeNotice && upgrade.state.kind === "idle" && (
            <button type="button" className={primary} onClick={update}>
              Update brindle
            </button>
          )}
          {upgrade.state.kind !== "idle" && (
            <div className="w-full">
              <UpgradeCard state={upgrade.state} onDismiss={upgrade.dismiss} />
            </div>
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
      </Section>

      <DebugLogging client={client} />

      <AppUpdate client={client} updater={appUpdater} release={releaseBuildUpdater} />

      <About read={readAbout} />

      {saved && dirty.length === 0 && (
        <p role="status" className={notice}>
          {saved}
        </p>
      )}
      {repo && dirty.length > 0 && (
        <div className={`${card} sticky bottom-0 flex flex-col gap-s1 border-branch px-s2 py-s2 shadow-lg`} data-testid="settings-save">
          <div className="flex flex-wrap items-center gap-s2">
            <span className="text-sm font-medium">{dirty.length === 1 ? "1 change" : `${dirty.length} changes`}</span>
            <fieldset className="flex flex-wrap items-center gap-1">
              <legend className={`${label} sr-only`}>Save for</legend>
              {SCOPES.map((s) => (
                <label
                  key={s.scope}
                  className={`flex cursor-pointer items-center gap-1 rounded-md border px-s1 py-1 text-sm ${target === s.scope ? "border-branch bg-branch-soft" : "border-line text-ink-2 hover:bg-surface-2"} ${pinned && s.scope !== "repo" ? "opacity-50" : ""}`}
                  title={s.detail}
                >
                  <input type="radio" name="settings-scope" value={s.scope} checked={target === s.scope} disabled={saving || (pinned && s.scope !== "repo")} onChange={() => setScope(s.scope)} className="accent-current" />
                  {s.label}
                </label>
              ))}
            </fieldset>
            <span className="flex-1" />
            <button type="button" className={quiet} disabled={saving} onClick={() => setDraft(loaded)}>
              Discard
            </button>
            <button type="button" className={primary} disabled={saving || missing.length > 0} onClick={() => void save()}>
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
          <p className="text-xs text-ink-3">{SCOPES.find((s) => s.scope === target)?.detail}</p>
          {pinned && (
            <p className="text-xs text-ink-3" data-testid="repo-only">
              {repoOnly.map((k) => SETTINGS.find((s) => s.key === k)?.label ?? k).join(", ")} only count from a repo's own files, so this save goes to this repo.
            </p>
          )}
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

    </section>
  );
}
