/* Organization governance (daemon/routes_governance.py) on Settings and
   Project settings. Settings › Organization: the status line (GET
   /v1/governance: "Managed by Acme · policy verified · from this device",
   or the exact problem and what to do), the offline policy export (POST
   /v1/governance/offline-policy, written to ~/.brindle/policy.json) and,
   for an Enterprise admin, a managed device file to save (POST
   /v1/governance/device-file). Plan approvals: Auto / Always / Never, the
   person's default on Settings and a project's override on Project
   settings, each saved the moment it is picked (PUT /v1/governance/plan).
   Nothing here asks the person to sign in first: the engine says what it
   knows and what to do. */

import { useEffect, useState } from "react";
import type { DaemonClient, Plan } from "../../api/client";
import type { DeviceFileDTO, GovernanceStatusDTO, PlanSettingDTO } from "../../api/schema";
import { alert, button, field, notice, quiet } from "../../components/ui";

const failure = (e: unknown) => (e instanceof Error ? e.message : String(e));

/* Where the signed policy came from, as the status line says it. */
const SOURCE: Record<string, string> = {
  device: "from this device",
  account: "from your account",
  snapshot: "from the last saved copy",
  offline: "from an offline policy export",
};

/* "Managed by Acme · policy verified · from this device"; null when there is no organization to name. */
export function statusLine(s: GovernanceStatusDTO): string | null {
  if (s.status === "unmanaged") return "Not managed by an organization.";
  const org = s.org_name ?? s.org_id;
  if (!org) return null;
  return [`Managed by ${org}`, s.verified ? "policy verified" : "policy not verified", s.source ? SOURCE[s.source] : null].filter(Boolean).join(" · ");
}

/* GET /v1/governance once, for the status line and whether the person may hand out device files. */
export function useGovernance(client: DaemonClient) {
  const [status, setStatus] = useState<GovernanceStatusDTO | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    client.governance().then(
      (s) => live && setStatus(s),
      (e: unknown) => live && setError(failure(e)),
    );
    return () => {
      live = false;
    };
  }, [client]);
  return { status, error };
}

export function OrgStatus({ status, error }: { status: GovernanceStatusDTO | null; error: string | null }) {
  if (error) return <p role="alert" className={alert}>Couldn't read your organization's policy: {error}</p>;
  if (!status) return <p className="text-sm text-ink-3">Checking your organization's policy…</p>;
  const line = statusLine(status);
  return (
    <div className="flex flex-col gap-1" data-testid="org-status" data-status={status.status} data-verified={!!status.verified} data-source={status.source ?? undefined}>
      {line && <p className="text-sm">{line}</p>}
      {status.problem && (
        <div role="alert" className={alert}>
          <p>{status.problem}</p>
          {status.action && <p className="text-ink">{status.action}</p>}
        </div>
      )}
    </div>
  );
}

/* An offline policy export: the signed policy for a device with no connection, good for 1 to 90 days. */
export function PolicyExport({ client, repo }: { client: DaemonClient; repo?: string | null }) {
  const [days, setDays] = useState(30);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const ok = !!repo && Number.isInteger(days) && days >= 1 && days <= 90;
  const run = async () => {
    setBusy(true);
    setDone(null);
    setError(null);
    try {
      const got = await client.exportOfflinePolicy(days, repo ?? undefined);
      setDone(`Saved a ${got.days}-day offline policy export to ${got.path}`);
    } catch (e) {
      setError(failure(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col gap-s1">
      <div className="flex flex-wrap items-center gap-s1">
        <label className="flex items-center gap-1 text-xs text-ink-3">
          Lasts
          <input type="number" min={1} max={90} className={`${field} w-20`} aria-label="Days the export lasts" value={Number.isNaN(days) ? "" : days} onChange={(e) => setDays(e.target.value === "" ? NaN : Number(e.target.value))} />
          days (at most 90)
        </label>
        <button type="button" className={button} disabled={busy || !ok} onClick={() => void run()}>
          {busy ? "Exporting…" : "Offline policy export"}
        </button>
        {!repo && <span className="text-xs text-ink-3">Pick a project first: the export is saved in its .brindle folder.</span>}
      </div>
      {done && <p role="status" className={notice}>{done}</p>}
      {error && <p role="alert" className={alert}>Could not export: {error}</p>}
    </div>
  );
}

/* A managed device file (managed.json) for one device: its name, whether it may run air-gapped, and where audit streaming sends. */
export function DeviceFile({ client }: { client: DaemonClient }) {
  const [name, setName] = useState("");
  const [airgap, setAirgap] = useState(false);
  const [sink, setSink] = useState("");
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState<DeviceFileDTO | null>(null);
  const [error, setError] = useState<string | null>(null);
  const run = async () => {
    setBusy(true);
    setFile(null);
    setError(null);
    try {
      setFile(await client.deviceFile({ name: name.trim(), allow_airgap: airgap, ...(sink.trim() ? { audit_sink: sink.trim() } : {}) }));
    } catch (e) {
      setError(failure(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col gap-s1">
      <div className="flex flex-wrap items-center gap-s1">
        <input className={field} aria-label="Device name" placeholder="Device name" maxLength={100} value={name} onChange={(e) => setName(e.target.value)} />
        <input className={field} aria-label="Audit streaming to" placeholder="Audit streaming to (optional)" value={sink} onChange={(e) => setSink(e.target.value)} />
        <label className="flex items-center gap-1 text-sm">
          <input type="checkbox" checked={airgap} onChange={(e) => setAirgap(e.target.checked)} />
          Allow air-gapped use
        </label>
        <button type="button" className={button} disabled={busy || !name.trim()} onClick={() => void run()}>
          Download device file
        </button>
      </div>
      {file && (
        <p className={notice}>
          <a className={`${quiet} underline`} href={`data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify(file.managed, null, 2))}`} download={file.filename} data-testid="device-file">
            Save {file.filename}
          </a>{" "}
          for device {file.device_id}.
        </p>
      )}
      {file && (
        <div className={notice} data-testid="install-location" data-path={file.system_dir}>
          <p>
            Have your MDM install it as <code>{file.system_dir}/{file.filename}</code> on that device:
          </p>
          <ol className="list-decimal pl-5">
            <li>Upload the file to your MDM as a managed file.</li>
            <li>Target it at the device's system folder above.</li>
            <li>Push it to the device; brindle does not install it itself.</li>
          </ol>
        </div>
      )}
      {error && <p role="alert" className={alert}>Could not make the device file: {error}</p>}
    </div>
  );
}

export const PLAN_HELP = "Auto plans first for heavy, cross-repo and CI/migration/auth/deploy changes.";
const PLANS: { value: Plan; label: string }[] = [
  { value: "auto", label: "Auto" },
  { value: "always", label: "Always" },
  { value: "never", label: "Never" },
];
const planLabel = (p: Plan) => PLANS.find((c) => c.value === p)?.label ?? p;

/* Plan approvals: without `repo` the person's default, with one that
   project's local override ("" = none: the shared config.json's, else theirs). */
export function PlanChoice({ client, repo, id }: { client: DaemonClient; repo?: string; id: string }) {
  const [setting, setSetting] = useState<PlanSettingDTO | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    client.planSetting(repo).then(
      (s) => live && setSetting(s),
      (e: unknown) => live && setError(failure(e)),
    );
    return () => {
      live = false;
    };
  }, [client, repo]);
  const pick = async (value: string) => {
    setBusy(true);
    setError(null);
    try {
      setSetting(await client.setPlan(repo ? { scope: "repo", repo, plan: value === "" ? null : (value as Plan) } : { scope: "user", plan: value as Plan }));
    } catch (e) {
      setError(failure(e));
    } finally {
      setBusy(false);
    }
  };
  if (!setting) return error ? <p role="alert" className={alert}>Couldn't read plan approvals: {error}</p> : <span className="text-sm text-ink-3">Loading…</span>;
  // "" clears the local override: back to the project's shared config.json when it sets one, else yours.
  const local = setting.project_source === "local" ? setting.project : null;
  return (
    <>
      <select id={id} className={field} value={repo ? (local ?? "") : setting.user} disabled={busy} onChange={(e) => void pick(e.target.value)}>
        {repo && <option value="">{setting.shared ? `Project's shared config: ${planLabel(setting.shared)}` : `Yours: ${planLabel(setting.user)}`}</option>}
        {PLANS.map((p) => (
          <option key={p.value} value={p.value}>
            {p.label}
          </option>
        ))}
      </select>
      {repo && setting.project_source === "shared" && (
        <p className="text-xs text-ink-3" data-testid="plan-shared">
          From the project's shared .brindle/config.json, committed for everyone on the repo. Picking another saves your own override to .brindle/config.local.json.
        </p>
      )}
      {error && <p role="alert" className={alert}>Could not save: {error}</p>}
    </>
  );
}
