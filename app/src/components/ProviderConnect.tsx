/* How a provider connects (design: "Connect providers redesign", boards
   1–4): three equal choices, Sign in / API key / Your cloud, the same in
   the first launch's Connect your providers and in Settings › Providers.

   API key: one paste field that tells the key's maker from its prefix
   (sk-ant- Anthropic, sk-or- OpenRouter, sk-proj- / sk- OpenAI, AIza
   Gemini), stores it write-only (PUT /v1/keys/{name}: the engine checks it
   with the provider first and answers its last four) and saves the method
   (PUT /v1/providers/{p}/connection). Verified or rejected shows with the
   last four, the billing it switches to, where to get a key, and Use NAME
   from my shell when the daemon's environment has one (POST
   /v1/keys/{name}/import). The value lives in the field until Save and
   nowhere after.

   Your cloud: Amazon Bedrock, Google Vertex AI, Microsoft Foundry (or
   OpenCode's Vertex and Azure), as the engine lists them for the provider
   (GET /v1/providers/{p}/connection's `clouds`), with what this machine's
   cloud CLIs are set up with (GET /v1/cloud/options) filled in. Sign in
   saves the route (PUT .../connection) and runs its cloud login as a job
   (POST .../cloud-sign-in), followed on the bus like brindle's own
   sign-in: the browser opens, the screen waits, Try again if it fails. */

import { useEffect, useRef, useState, type ReactNode } from "react";
import { DaemonError, type DaemonClient, type KeyInfo } from "../api/client";
import { openExternal, type OpenUrl } from "../api/open";
import type { CloudOptionsDTO, ProviderConnectionDTO } from "../api/schema";
import { plain } from "../api/wording";
import { useJob } from "../screens/Branch/useJob";
import { alert, button, field, mono, quiet } from "./ui";

export type Method = "sign_in" | "api_key" | "cloud";

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));
const link = "text-branch underline-offset-2 hover:underline";
const fine = "text-xs text-ink-3";

/* ---- The three choices. ---- */

const BLURB: Record<string, Record<Method, string>> = {
  claude: {
    sign_in: "Use your Claude Pro, Max or Team plan. Opens your browser.",
    api_key: "Paste a key from the Claude Console. Billed per use.",
    cloud: "Amazon Bedrock, Google Vertex AI or Microsoft Foundry, with your cloud login.",
  },
  codex: {
    sign_in: "Use your ChatGPT plan. Opens your browser.",
    api_key: "Paste a key from the OpenAI platform. Billed per use.",
    cloud: "Through your own cloud, with your cloud login.",
  },
  antigravity: {
    sign_in: "Use your Google account. Opens your browser.",
    api_key: "Paste a Gemini API key from Google AI Studio. Billed per use.",
    cloud: "Through your own cloud, with your cloud login.",
  },
  opencode: {
    sign_in: "Sign in to your model provider through OpenCode. Opens your browser.",
    api_key: "Paste a key for your model provider. Billed per use.",
    cloud: "Google Vertex AI or Azure OpenAI, with your cloud login.",
  },
};
const METHODS: { method: Method; title: string }[] = [
  { method: "sign_in", title: "Sign in" },
  { method: "api_key", title: "API key" },
  { method: "cloud", title: "Your cloud" },
];

export function MethodChoice({ provider, name, value, disabled = false, onChange }: { provider: string; name: string; value: Method | null; disabled?: boolean; onChange(method: Method): void }) {
  const blurb = BLURB[provider] ?? BLURB.claude;
  return (
    <div role="radiogroup" aria-label={`How to connect ${name}`} data-testid="connect-method" data-method={value ?? ""} className="grid gap-s1 sm:grid-cols-3">
      {METHODS.map(({ method, title }) => (
        <button
          key={method}
          type="button"
          role="radio"
          aria-checked={value === method}
          aria-labelledby={`${provider}-${method}-title`}
          aria-describedby={`${provider}-${method}-blurb`}
          disabled={disabled}
          className="flex flex-col items-start gap-1 rounded-md border border-line-strong p-s2 text-left hover:bg-surface-2 disabled:opacity-50 aria-checked:border-branch aria-checked:bg-surface-2"
          onClick={() => onChange(method)}
        >
          <span id={`${provider}-${method}-title`} className="text-sm font-semibold">
            {title}
          </span>
          <span id={`${provider}-${method}-blurb`} className="text-[13px] leading-snug text-ink-2">
            {blurb[method]}
          </span>
        </button>
      ))}
    </div>
  );
}

/* ---- API keys. ---- */

/* Whose key a pasted value is, by its prefix (the most specific first). */
const PREFIXES: [string, string][] = [
  ["sk-ant-", "ANTHROPIC_API_KEY"],
  ["sk-or-", "OPENROUTER_API_KEY"],
  ["sk-proj-", "OPENAI_API_KEY"],
  ["AIza", "GEMINI_API_KEY"],
  ["sk-", "OPENAI_API_KEY"],
];
export function recognize(value: string): string | null {
  const v = value.trim();
  return PREFIXES.find(([prefix]) => v.startsWith(prefix))?.[1] ?? null;
}

interface KeyFacts {
  maker: string;
  /* Where the key is billed, and the plan it replaces. */
  billing: string;
  /* Where to get one (the engine's key_page wins when it gives one). */
  page: string;
  console: string;
}
export const KEYS: Record<string, KeyFacts> = {
  ANTHROPIC_API_KEY: { maker: "Anthropic", billing: "Billed per use to your Console account, instead of a Claude plan.", page: "https://console.anthropic.com/settings/keys", console: "the Claude Console" },
  OPENAI_API_KEY: { maker: "OpenAI", billing: "Billed per use to your OpenAI platform account, instead of a ChatGPT plan.", page: "https://platform.openai.com/api-keys", console: "the OpenAI platform" },
  CODEX_API_KEY: { maker: "OpenAI", billing: "Billed per use to your OpenAI platform account, instead of a ChatGPT plan.", page: "https://platform.openai.com/api-keys", console: "the OpenAI platform" },
  GEMINI_API_KEY: { maker: "Gemini", billing: "Billed per use to your Google AI Studio project.", page: "https://aistudio.google.com/apikey", console: "Google AI Studio" },
  OPENROUTER_API_KEY: { maker: "OpenRouter", billing: "Billed per use to your OpenRouter credits.", page: "https://openrouter.ai/keys", console: "OpenRouter" },
};
const factsOfKey = (name: string): KeyFacts => KEYS[name] ?? { maker: name, billing: "Billed per use to the account the key belongs to.", page: "", console: "its provider" };

/* The keys each provider reads, the first the one a paste goes to. */
export const PROVIDER_KEYS: Record<string, string[]> = {
  claude: ["ANTHROPIC_API_KEY"],
  codex: ["OPENAI_API_KEY", "CODEX_API_KEY"],
  antigravity: ["GEMINI_API_KEY"],
  opencode: ["OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
};

/* OpenCode runs the model provider picked first: by key, or through a cloud. */
export const MODEL_PROVIDERS: { id: string; name: string; key?: string; cloud?: string }[] = [
  { id: "openrouter", name: "OpenRouter", key: "OPENROUTER_API_KEY" },
  { id: "anthropic", name: "Anthropic", key: "ANTHROPIC_API_KEY" },
  { id: "openai", name: "OpenAI", key: "OPENAI_API_KEY" },
  { id: "google-vertex", name: "Google Vertex AI", cloud: "vertex" },
  { id: "azure", name: "Azure OpenAI", cloud: "azure" },
];

export function ModelProviderPicker({ value, onChange }: { value: string; onChange(id: string): void }) {
  return (
    <label className="flex flex-wrap items-center gap-s1 text-sm text-ink-2">
      Model provider
      <select className={`${field} min-w-48`} value={value} onChange={(e) => onChange(e.target.value)}>
        {MODEL_PROVIDERS.map((m) => (
          <option key={m.id} value={m.id}>
            {m.name}
          </option>
        ))}
      </select>
    </label>
  );
}

/* "just now", "4 min ago", "2h ago", "3d ago" since `ts` (seconds). */
export function ago(ts: number | null | undefined, now = Date.now() / 1000): string {
  if (ts == null) return "";
  const m = Math.floor(Math.max(0, now - ts) / 60);
  return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.floor(m / 60)}h ago` : `${Math.floor(m / 1440)}d ago`;
}

type KeyOutcome = { kind: "stored"; name: string; last4: string; status: KeyInfo["status"]; checked_at?: number | null } | { kind: "rejected"; name: string; why: string };

/* The key's state in one line: recognized and verified (or unchecked) with its last four, or rejected and why. */
function KeyState({ outcome }: { outcome: KeyOutcome }) {
  const f = factsOfKey(outcome.name);
  if (outcome.kind === "rejected") {
    return (
      <span className="text-fail" data-testid="key-state" data-status="rejected">
        <span aria-hidden>✕ </span>
        {f.maker} rejected this key: {outcome.why}
      </span>
    );
  }
  if (outcome.status === "rejected") {
    return (
      <span className="text-fail" data-testid="key-state" data-status="rejected">
        <span aria-hidden>✕ </span>
        {f.maker} rejected the key ending <span className={mono}>{outcome.last4}</span>
        {outcome.checked_at ? ` ${ago(outcome.checked_at)}` : ""}
      </span>
    );
  }
  const checked = outcome.status === "verified" ? `verified ${ago(outcome.checked_at) || "just now"}` : "saved, not checked";
  return (
    <span data-testid="key-state" data-status={outcome.status ?? "unchecked"}>
      <span aria-hidden className="text-pass">
        ✓{" "}
      </span>
      {f.maker} key recognized · {checked} · ending <span className={mono}>{outcome.last4}</span>
    </span>
  );
}

/* The paste field. `expect`: the key the provider reads; `any`: a key of
   any recognized maker goes under its own name (OpenCode). */
export function ApiKeyPaste({ client, provider, expect, any = false, keys, repo, openUrl = openExternal, onSaved, onCancel }: {
  client: DaemonClient;
  provider: string;
  expect: string;
  any?: boolean;
  /* GET /config's keys: a stored key's state, where to get one, whether the shell has one. */
  keys: KeyInfo[] | null;
  repo?: string | null;
  openUrl?: OpenUrl;
  onSaved?(name: string, last4: string): void;
  onCancel?(): void;
}) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<KeyOutcome | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refused, setRefused] = useState<string | null>(null);
  const seen = recognize(value);
  const mismatch = !!seen && !any && seen !== expect && !(PROVIDER_KEYS[provider] ?? []).includes(seen);
  const name = seen && !mismatch ? (any || (PROVIDER_KEYS[provider] ?? []).includes(seen) ? seen : expect) : expect;
  const info = keys?.find((k) => k.name === name);
  const f = factsOfKey(name);
  const page = info?.key_page || f.page;

  /* Stored: the method saved as an API key, and the key's state as the engine now reads it. */
  const stored = async (key: string, last4: string) => {
    const got = await client.config(repo ?? undefined).then((c) => c.keys.find((k) => k.name === key), () => undefined);
    setOutcome({ kind: "stored", name: key, last4: got?.last4 || last4, status: got?.status ?? "verified", checked_at: got?.checked_at ?? Date.now() / 1000 });
    try {
      await client.setProviderConnection(provider, { method: "api_key", ...(repo ? { repo } : {}) });
    } catch (e) {
      // The key is stored; the org (or the engine) refuses running the provider on it.
      setRefused(message(e));
      return;
    }
    onSaved?.(key, last4);
  };
  const run = async (key: string, act: () => Promise<{ last4: string }>) => {
    setBusy(true);
    setError(null);
    setRefused(null);
    setOutcome(null);
    try {
      const done = await act();
      setValue("");
      await stored(key, done.last4);
    } catch (e) {
      // The engine checks the key with its provider first: a refusal is the key's, not the app's.
      if (e instanceof DaemonError && e.code === "not_stored") setOutcome({ kind: "rejected", name: key, why: plain(e.message.replace(new RegExp(`^${key} not stored: `), "")) });
      else setError(message(e));
    } finally {
      setBusy(false);
    }
  };
  const save = () => {
    if (!value.trim() || busy || mismatch) return;
    void run(name, () => client.putKey(name, value.trim(), repo ?? undefined));
  };
  const importable = (any ? (PROVIDER_KEYS[provider] ?? [expect]) : [expect]).map((n) => keys?.find((k) => k.name === n && k.importable)).find(Boolean);
  const current = info?.set ? { kind: "stored" as const, name, last4: info.last4, status: info.status, checked_at: info.checked_at } : null;
  const shown = outcome ?? current;

  return (
    <form
      className="flex flex-col gap-s1"
      aria-label={`API key for ${provider}`}
      data-testid="api-key-paste"
      onSubmit={(e) => {
        e.preventDefault();
        save();
      }}
    >
      <label htmlFor={`${provider}-key`} className="text-sm text-ink-2">
        Paste your key
      </label>
      <div className="flex flex-wrap items-center gap-s1">
        <input
          id={`${provider}-key`}
          className={`${field} min-w-64 flex-1 font-mono`}
          type="password"
          autoComplete="off"
          placeholder={info?.set ? `Replace the key ending ${info.last4}` : `${f.maker} API key`}
          value={value}
          disabled={busy}
          onChange={(e) => setValue(e.target.value)}
        />
        <button type="submit" className={button} disabled={busy || !value.trim() || mismatch}>
          {busy ? "Checking…" : "Save"}
        </button>
        {onCancel && (
          <button type="button" className={quiet} disabled={busy} onClick={onCancel}>
            Cancel
          </button>
        )}
      </div>
      <div className="flex flex-col gap-1 text-[13px]" role="status">
        {value.trim() && !busy && (mismatch ? <span className="text-sap">That looks like a key for {factsOfKey(seen!).maker}; this needs one for {f.maker}.</span> : seen && !outcome && <span className="text-ink-2">{f.maker} key recognized.</span>)}
        {shown && <KeyState outcome={shown} />}
        <span className="text-sap">{f.billing}</span>
        <span className={fine}>
          Stored in your keychain.
          {page && (
            <>
              {" "}
              <button type="button" className={link} onClick={() => void openUrl(page).catch(() => undefined)}>
                Get a key in {f.console}
              </button>
            </>
          )}
          {importable && (
            <>
              {" · "}
              <button type="button" className={link} disabled={busy} onClick={() => void run(importable.name, () => client.importKey(importable.name, repo ?? undefined))}>
                Use {importable.name} from my shell
              </button>
            </>
          )}
        </span>
      </div>
      {error && (
        <p className={alert} role="alert">
          Couldn't store the key: {error}
        </p>
      )}
      {refused && (
        <p className={alert} role="alert">
          The key is stored, but it can't be used for this provider: {refused}
        </p>
      )}
    </form>
  );
}

/* ---- Your cloud. ---- */

export const CLOUD_NAME: Record<string, string> = { bedrock: "Amazon Bedrock", vertex: "Google Vertex AI", foundry: "Microsoft Foundry", azure: "Azure OpenAI" };
const CLOUD_ACCOUNT: Record<string, { who: string; signIn: string }> = {
  bedrock: { who: "AWS", signIn: "Sign in with AWS" },
  vertex: { who: "Google Cloud", signIn: "Sign in with Google Cloud" },
  foundry: { who: "Azure", signIn: "Sign in with Azure" },
  azure: { who: "Azure", signIn: "Sign in with Azure" },
};

interface CloudField {
  name: string;
  label: string;
  kind: "profile" | "region" | "text";
  required?: boolean;
  /* What this machine's CLIs already say. */
  seed?(o: CloudOptionsDTO, settings: Record<string, string>): string | null | undefined;
}
/* The settings brindle.connections.ROUTES accepts for each provider's route. */
const FIELDS: Record<string, CloudField[]> = {
  "claude/bedrock": [
    { name: "AWS_PROFILE", label: "AWS profile", kind: "profile", seed: (o) => (o.aws_profiles.find((p) => p.sso) ?? o.aws_profiles[0])?.name },
    { name: "AWS_REGION", label: "Region", kind: "region", required: true, seed: (o, s) => o.aws_profiles.find((p) => p.name === s.AWS_PROFILE)?.region ?? o.regions?.bedrock?.[0] },
  ],
  "claude/vertex": [
    { name: "ANTHROPIC_VERTEX_PROJECT_ID", label: "Project", kind: "text", required: true, seed: (o) => o.gcloud.project },
    { name: "CLOUD_ML_REGION", label: "Region", kind: "region", required: true, seed: (o) => o.gcloud.region ?? o.regions?.vertex?.[0] },
  ],
  "claude/foundry": [
    { name: "ANTHROPIC_FOUNDRY_RESOURCE", label: "Foundry resource", kind: "text", required: true },
    { name: "AZURE_TENANT_ID", label: "Tenant", kind: "text", seed: (o) => o.azure?.tenant_id },
  ],
  "opencode/vertex": [
    { name: "GOOGLE_CLOUD_PROJECT", label: "Project", kind: "text", required: true, seed: (o) => o.gcloud.project },
    { name: "VERTEX_LOCATION", label: "Region", kind: "region", seed: (o) => o.gcloud.region ?? o.regions?.vertex?.[0] },
  ],
  "opencode/azure": [
    { name: "AZURE_RESOURCE_NAME", label: "Azure OpenAI resource", kind: "text", required: true },
    { name: "AZURE_TENANT_ID", label: "Tenant", kind: "text", seed: (o) => o.azure?.tenant_id },
    { name: "AZURE_SUBSCRIPTION_ID", label: "Subscription", kind: "text", seed: (o) => o.azure?.subscription_id },
  ],
};

/* What the machine's CLIs were found set up with, for the route. */
function detected(route: string, o: CloudOptionsDTO): string {
  if (route === "bedrock") return o.aws_profiles.length ? `Found ${o.aws_profiles.length === 1 ? "1 AWS profile" : `${o.aws_profiles.length} AWS profiles`} in ~/.aws/config.` : "No AWS profiles found; brindle uses your default AWS credentials.";
  if (route === "vertex") return o.gcloud.adc ? `gcloud is signed in${o.gcloud.project ? `, project ${o.gcloud.project}` : ""}.` : "gcloud isn't signed in yet: Sign in does it.";
  return o.azure ? `The Azure CLI is signed in${o.azure.subscription_name ? ` to ${o.azure.subscription_name}` : ""}.` : "The Azure CLI isn't signed in yet: Sign in does it.";
}

/* The cloud sign-in's job: the browser opens on the first URL it prints;
   done, its result says how it went. */
function CloudSignIn({ client, provider, route, openUrl, save, label, onSignedIn }: { client: DaemonClient; provider: string; route: string; openUrl: OpenUrl; save(): Promise<boolean>; label?: string; onSignedIn?(): void }) {
  const { job, busy, start, clear } = useJob();
  const opened = useRef<string | null>(null);
  const url = job?.status === "running" ? (job.progress.find((line) => /^https?:\/\//.test(line)) ?? null) : null;
  useEffect(() => {
    if (!url || opened.current === url) return;
    opened.current = url;
    void openUrl(url).catch(() => undefined);
  }, [url, openUrl]);
  const result = job?.status === "done" ? String((job.result as { result?: unknown } | undefined)?.result ?? "") : null;
  useEffect(() => {
    if (result === "signed in") onSignedIn?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result]);
  const cloud = CLOUD_ACCOUNT[route] ?? { who: "your cloud", signIn: "Sign in" };
  const begin = async () => {
    opened.current = null;
    clear();
    if (await save()) void start(`Sign in to ${CLOUD_NAME[route] ?? route}`, () => client.cloudSignIn(provider));
  };
  const failed = job?.status === "failed" || result === "failed";
  return (
    <div className="flex flex-col gap-s1" data-testid="cloud-sign-in" data-state={result ?? job?.status ?? "idle"}>
      <div className="flex flex-wrap items-center gap-s2">
        {busy ? (
          <span className="inline-flex items-center gap-s1 text-sm" role="status">
            <span className="size-3.5 shrink-0 animate-spin rounded-full border-2 border-branch border-t-transparent" aria-hidden />
            Waiting for the browser…
          </span>
        ) : result === "signed in" ? (
          <span className="text-sm" role="status">
            <span aria-hidden className="text-pass">
              ✓{" "}
            </span>
            Signed in through {CLOUD_NAME[route] ?? route}
          </span>
        ) : (
          <button type="button" className={button} onClick={() => void begin()}>
            {failed ? "Try again" : (label ?? cloud.signIn)}
          </button>
        )}
        {url && busy && (
          <button type="button" className={`${quiet} ${link}`} onClick={() => void openUrl(url).catch(() => undefined)}>
            Open the tab again
          </button>
        )}
        {!busy && !result && !failed && !label && <span className={fine}>Opens your cloud's own sign-in in your browser. No {provider === "claude" ? "Anthropic" : "model provider"} account needed.</span>}
      </div>
      {busy && job && job.progress.length > 0 && <p className={`${mono} truncate text-ink-3`}>{job.progress.at(-1)}</p>}
      {result === "waiting" && <p className={fine}>A sign-in for this login is already running: finish it in your browser.</p>}
      {result === "manual" && <p className={fine}>This login can't be started from here: sign in with your cloud's own tools, then come back.</p>}
      {failed && (
        <p className={alert} role="alert">
          The sign-in didn't finish{job?.error ? `: ${plain(job.error)}` : "."}
        </p>
      )}
      {!label && <span className={fine}>Billed through your {cloud.who} account. brindle renews the login when it lapses and tells you before it does.</span>}
    </div>
  );
}

/* Choose the route, fill in its settings from what the machine has, then sign in. */
export function CloudSetup({ client, provider, name, route: wanted, repo, openUrl = openExternal, onSignedIn }: {
  client: DaemonClient;
  provider: string;
  name: string;
  /* A route to start on (OpenCode's model provider picks one). */
  route?: string;
  repo?: string | null;
  openUrl?: OpenUrl;
  onSignedIn?(route: string): void;
}) {
  const [connection, setConnection] = useState<ProviderConnectionDTO | null>(null);
  const [options, setOptions] = useState<CloudOptionsDTO | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [route, setRoute] = useState<string | null>(wanted ?? null);
  const [settings, setSettings] = useState<Record<string, string>>({});
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    Promise.all([client.providerConnection(provider, repo ?? undefined), client.cloudOptions()]).then(
      ([c, o]) => {
        if (!live) return;
        setConnection(c);
        setOptions(o);
        setRoute((r) => r ?? c.choice?.cloud ?? c.cloud ?? c.clouds?.[0] ?? null);
      },
      (e: unknown) => live && setLoadError(message(e)),
    );
    return () => {
      live = false;
    };
  }, [client, provider, repo]);
  useEffect(() => {
    if (wanted) setRoute(wanted);
  }, [wanted]);

  // A route's fields start from what is saved for it, else from what the CLIs say.
  const fields = route ? (FIELDS[`${provider}/${route}`] ?? []) : [];
  useEffect(() => {
    if (!route || !options || !connection) return;
    const saved = connection.choice?.cloud === route ? (connection.choice.settings ?? {}) : connection.cloud === route ? (connection.settings ?? {}) : {};
    const next: Record<string, string> = {};
    for (const f of FIELDS[`${provider}/${route}`] ?? []) next[f.name] = saved[f.name] ?? f.seed?.(options, next) ?? "";
    setSettings(next);
  }, [route, options, connection, provider]);

  if (loadError) return <p className={alert}>Couldn't read your cloud setup: {loadError}</p>;
  if (!connection || !options) return <p className={fine}>Looking at your cloud setup…</p>;
  const clouds = connection.clouds ?? [];
  if (clouds.length === 0) {
    return (
      <p className="text-sm text-ink-2" data-testid="cloud-setup">
        brindle can't run {name} through a cloud yet. OpenCode can, with Google Vertex AI or Azure OpenAI.
      </p>
    );
  }
  const missing = fields.filter((f) => f.required && !settings[f.name]?.trim());
  const save = async () => {
    if (!route || missing.length) return false;
    setSaveError(null);
    try {
      const got = await client.setProviderConnection(provider, { method: "cloud", cloud: route, settings: Object.fromEntries(Object.entries(settings).filter(([, v]) => v.trim())), ...(repo ? { repo } : {}) });
      setConnection(got);
      return true;
    } catch (e) {
      setSaveError(message(e));
      return false;
    }
  };
  const input = (f: CloudField): ReactNode => {
    const set = (v: string) => setSettings((s) => ({ ...s, [f.name]: v }));
    const value = settings[f.name] ?? "";
    if (f.kind === "profile") {
      return (
        <select className={field} value={value} onChange={(e) => set(e.target.value)}>
          <option value="">Default credentials</option>
          {options.aws_profiles.map((p) => (
            <option key={p.name} value={p.name}>
              {p.name}
              {p.sso ? " (SSO, from ~/.aws/config)" : " (from ~/.aws/config)"}
            </option>
          ))}
        </select>
      );
    }
    const regions = route ? (options.regions?.[route] ?? []) : [];
    if (f.kind === "region" && regions.length) {
      return (
        <select className={field} value={value} onChange={(e) => set(e.target.value)}>
          {!value && <option value="">Choose a region</option>}
          {[...new Set([...(value ? [value] : []), ...regions])].map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
      );
    }
    return <input className={`${field} font-mono`} value={value} placeholder={f.required ? "required" : "optional"} onChange={(e) => set(e.target.value)} />;
  };

  return (
    <div className="flex flex-col gap-s2" data-testid="cloud-setup" data-route={route ?? ""}>
      {connection.managed && <p className={fine}>{plain(connection.managed)}</p>}
      <div role="radiogroup" aria-label="Cloud" className="flex flex-wrap gap-s1">
        {clouds.map((c) => (
          <button key={c} type="button" role="radio" aria-checked={route === c} className="rounded-md border border-line-strong px-s2 py-1 text-sm text-ink-2 hover:bg-surface-2 aria-checked:border-branch aria-checked:bg-surface-2 aria-checked:text-ink" onClick={() => setRoute(c)}>
            {CLOUD_NAME[c] ?? c}
          </button>
        ))}
      </div>
      {route && (
        <>
          <div className="grid gap-s1 sm:grid-cols-2">
            {fields.map((f) => (
              <label key={f.name} className="flex flex-col gap-1 text-sm text-ink-2">
                {f.label}
                {input(f)}
              </label>
            ))}
          </div>
          <p className={fine}>Detected: {detected(route, options)}</p>
          {missing.length > 0 ? (
            <p className={fine}>Fill in {missing.map((f) => f.label.toLowerCase()).join(" and ")} to sign in.</p>
          ) : (
            <CloudSignIn key={route} client={client} provider={provider} route={route} openUrl={openUrl} save={save} onSignedIn={() => onSignedIn?.(route)} />
          )}
          {saveError && (
            <p className={alert} role="alert">
              Couldn't use {CLOUD_NAME[route] ?? route}: {saveError}
            </p>
          )}
        </>
      )}
    </div>
  );
}

/* The cloud login's sign-in alone, for Renew login on a row already set up. */
export function RenewLogin({ client, provider, route, openUrl = openExternal, onSignedIn }: { client: DaemonClient; provider: string; route: string; openUrl?: OpenUrl; onSignedIn?(): void }) {
  return <CloudSignIn client={client} provider={provider} route={route} openUrl={openUrl} save={async () => true} label="Renew login" onSignedIn={onSignedIn} />;
}
