/* What a provider's CLI needs before brindle can launch it, as native UI
   instead of a launch that would stop at the CLI's own prompt: a sign-in
   (ProviderSignIn, browser first), a key (a password field, PUT
   /v1/keys/{name}), the CLI's trust of the folder or a consent it asks for
   once (a button, POST /v1/providers/{p}/preflight/{item}). The engine
   answers GET /v1/providers/{p}/preflight (branch desktop/provider-preflight)
   and refuses a start or continue with 409 {preflight} while an item is
   open; this card shows that answer, settles the items, and offers the
   launch again once every item reads done. */

import { useState, type FormEvent } from "react";
import { useDaemon } from "../api/context";
import type { PreflightDTO, PreflightItem } from "../api/client";
import { providerName } from "../store/overview";
import { ProviderSignIn } from "./ProviderSignIn";
import { alert, button, card, field, primary } from "./ui";

export interface PreflightProps {
  preflight: PreflightDTO;
  /* The items again, after one was settled (GET /providers/{p}/preflight). */
  onRefresh(next: PreflightDTO): void;
  /* Every item is done: launch again. */
  onLaunch(): void;
  /* What the launch is: "Start" or "Continue". */
  launchLabel?: string;
  /* The repo (a sign-in's check). */
  repo?: string | null;
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

function KeyField({ item, provider, onDone }: { item: PreflightItem; provider: string; onDone(): void }) {
  const client = useDaemon();
  const name = item.key_name ?? "API_KEY";
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await client.putKey(name, value);
      setValue("");
      onDone();
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="flex flex-col gap-1" aria-label={`Value for ${name}`} onSubmit={submit}>
      <div className="flex gap-s1">
        <input type="password" className={`${field} w-full font-mono`} aria-label={`${name} value`} placeholder={`${name} for ${providerName(provider)}`} value={value} autoComplete="off" onChange={(e) => setValue(e.target.value)} />
        <button type="submit" className={primary} disabled={busy || !value.trim()}>
          Save
        </button>
      </div>
      {error && (
        <p className={alert} role="alert">
          {error}
        </p>
      )}
    </form>
  );
}

function Settle({ item, provider, onDone, label, repo }: { item: PreflightItem; provider: string; onDone(next: PreflightDTO): void; label: string; repo?: string | null }) {
  const client = useDaemon();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      onDone(await client.settlePreflight(provider, item.id, item.action, repo));
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-wrap items-center gap-s1">
      <button type="button" className={button} disabled={busy} onClick={() => void run()}>
        {label}
      </button>
      {error && <span className="text-sm text-sap">{error}</span>}
    </div>
  );
}

const KIND_LABEL: Record<PreflightItem["kind"], string> = { signin: "Sign in", key: "API key", trust: "Trust", consent: "Consent" };

export function Preflight({ preflight, onRefresh, onLaunch, launchLabel = "Start", repo }: PreflightProps) {
  const client = useDaemon();
  const { provider, items } = preflight;
  const name = providerName(provider);
  const open = items.filter((i) => !i.done);
  const refresh = () => void client.preflight(provider, repo).then(onRefresh).catch(() => undefined);
  return (
    <section className={`${card} flex w-full max-w-xl flex-col gap-s2 px-s3 py-s2`} aria-label={`Before ${name} can start`} data-testid="preflight">
      <div>
        <h2 className="text-base font-semibold">
          {open.length === 0 ? `${name} is ready` : `Before ${name} can start`}
        </h2>
        <p className="text-sm text-ink-2">
          {open.length === 0 ? "Everything it asked for is settled." : `${name} asks for ${open.length === 1 ? "one thing" : `${open.length} things`} first. Nothing here opens a terminal.`}
        </p>
      </div>
      <ul className="flex flex-col gap-s2">
        {items.map((item) => (
          <li key={item.id} className="flex flex-col gap-1" data-testid={`preflight-${item.kind}`} data-done={item.done}>
            <div className="flex items-center gap-s1">
              <span className={`flex size-4 shrink-0 items-center justify-center rounded-full text-[10px] font-semibold ${item.done ? "bg-pass text-on-branch" : "border border-line-strong text-ink-3"}`} aria-hidden>
                {item.done ? "✓" : ""}
              </span>
              <span className="text-xs font-medium uppercase tracking-wider text-ink-3">{KIND_LABEL[item.kind]}</span>
              <span className={`text-sm ${item.done ? "text-ink-3 line-through" : "font-medium"}`}>{item.label}</span>
            </div>
            {!item.done && item.detail && <p className="pl-6 text-sm text-ink-2">{item.detail}</p>}
            {!item.done && (
              <div className="pl-6">
                {item.kind === "signin" && (
                  <ProviderSignIn
                    provider={provider}
                    check={async () => {
                      const next = await client.preflight(provider, repo);
                      return !next.items.some((i) => i.id === item.id && !i.done);
                    }}
                    onDone={refresh}
                  />
                )}
                {item.kind === "key" && <KeyField item={item} provider={provider} onDone={refresh} />}
                {item.kind === "trust" && <Settle item={item} provider={provider} onDone={onRefresh} label="Trust this folder" repo={repo} />}
                {item.kind === "consent" && <Settle item={item} provider={provider} onDone={onRefresh} label="Agree" repo={repo} />}
              </div>
            )}
          </li>
        ))}
      </ul>
      <div className="flex items-center gap-s1">
        <button type="button" className={primary} disabled={open.length > 0} onClick={onLaunch}>
          {launchLabel}
        </button>
        {open.length > 0 && <span className="text-xs text-ink-3">{repo ? `in ${repo}` : ""}</span>}
      </div>
    </section>
  );
}
