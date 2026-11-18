/* The Inbox card for brindle's offer to move an out-of-quota supervisor to
   another provider (kind "provider_switch"), or back once the original
   provider's limit resets (kind "provider_switch_back"). The daemon sends it
   as an ordinary InboxItemDTO (docs/provider-switch-design.md, decision 9):
   request_id is the offer id, detail reads "from → to", and action is
   POST /v1/sessions/{root}/provider-switch, which takes {offer, accept, always?}.
   One card stands for every session on one limit: its detail then ends
   "· <n> sessions: <ids>", and one answer goes to all of them.
   Switch is a LoadingButton, so a second press while the first is out does
   nothing; the engine also answers a repeated Switch with the supervisor the
   first one started. Once either answer is accepted the card leaves. */

import { isTauri } from "@tauri-apps/api/core";
import { useRef, useState } from "react";
import type { DaemonClient } from "../../api/client";
import { tauriFetch } from "../../api/tauri";
import type { InboxItemDTO } from "../../api/schema";
import { providerLabel } from "../../api/providers";
import { LoadingButton } from "../LoadingButton";
import { button, quiet } from "../ui";

export interface ProviderSwitchOffer {
  offerId: string;
  rootId: string;
  kind: "switch" | "back";
  fromProvider: string;
  toProvider: string;
  /* When fromProvider's limit lifts (switch), epoch seconds; null for a back offer. */
  limitedUntil: number | null;
  /* A notice's "HH:MM" reset time, as the daemon formatted it. */
  resetAt?: string;
  /* Every session this one card answers for: the engine shows one card per
     limit, not per session (provider_switch.inbox_items). Empty for one. */
  sessions?: string[];
  /* Each listed session's own target, by id; `toProvider` "+"-joins them when they differ. */
  sessionTargets?: Record<string, string>;
  /* Workers paused on the same limit, waiting on the person (engine brindle.handover), each as the
     engine words it: its replacement and whether that bills per token. */
  workers?: string[];
}

export interface ProviderSwitchCardProps {
  offer: ProviderSwitchOffer;
  variant?: "offer" | "notice";
  /* True for Switch (Switch back now on a notice), false for Not now (Dismiss). */
  onAnswer(accept: boolean, always?: boolean): Promise<unknown>;
  /* The workers' answers (the item's choices: continue / wait / stop), each going to every worker listed. */
  workerChoices?: { id: string; label: string }[];
  onWorkers?(choice: string): Promise<unknown>;
}

export const NOTICE_PREFIX = "notice: ";
export const isNotice = (item: InboxItemDTO) => (item.detail ?? "").startsWith(NOTICE_PREFIX);

export const PROVIDER_SWITCH_KINDS =["provider_switch", "provider_switch_back"];

/* A card for several sessions goes on "· <n> sessions: <id>, <id>", then
   "· <n> workers: <id>" for the workers paused on the same limit. */
const MEMBERS = /^\d+ (sessions|workers?): (.+)$/;

/* The offer an Inbox item carries, from its existing fields. */
export function offerOf(item: InboxItemDTO): ProviderSwitchOffer {
  const detail = item.detail ?? "";
  const notice = detail.startsWith(NOTICE_PREFIX);
  /* A notice reads "notice: from → to · HH:MM"; an offer is just "from → to". Either may end with its members. */
  const parts = (notice ? detail.slice(NOTICE_PREFIX.length) : detail).split(/\s*·\s*/);
  const members: Record<string, string[]> = {};
  for (let m; parts.length > 1 && (m = MEMBERS.exec(parts[parts.length - 1])); parts.pop()) {
    members[m[1].startsWith("worker") ? "workers" : "sessions"] = m[2].split(/\s*,\s*/);
  }
  const [pair, resetAt] = parts;
  const [from, to] = pair.split(/\s*→\s*/);
  return {
    /* "<id> → <target>": sessions on one limit can move to different providers. */
    sessions: (members.sessions ?? []).map((s) => s.split(/\s*→\s*/)[0]),
    sessionTargets: Object.fromEntries((members.sessions ?? []).map((s) => s.split(/\s*→\s*/)).filter((p) => p[1]).map(([id, t]) => [id, t])),
    workers: members.workers ?? [],
    resetAt: notice ? resetAt : undefined,
    offerId: item.request_id ?? "",
    rootId: item.root_id ?? "",
    kind: item.kind === "provider_switch_back" ? "back" : "switch",
    fromProvider: from ?? "",
    toProvider: to ?? "",
    limitedUntil: null,
  };
}

/* The line the card leads with. A back offer names the provider with quota again (the one it returns to). */
export function switchLine(offer: ProviderSwitchOffer, variant: "offer" | "notice" = "offer"): string {
  const n = offer.sessions?.length ?? 0;
  const them = n > 1 ? ` ${n} sessions` : "";
  /* Sessions on one limit may go to different providers: name every one (each session's own is listed below). */
  const targets = offerTargets(offer);
  const each = targets.includes(" and ") ? ", each to its own" : "";
  if (variant === "notice") {
    const backAt = offer.resetAt ? `, back at ${offer.resetAt}` : offer.limitedUntil === null ? "" : `, back at ${new Date(offer.limitedUntil * 1000).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }).replace(/\s/g, "").toLowerCase()}`;
    return `Switched${them} to ${targets}${each} · ${providerLabel(offer.fromProvider)} was out of quota${backAt}`;
  }
  return offer.kind === "back"
    ? `${targets} has quota again, switch${them} back`
    : `Out of quota on ${providerLabel(offer.fromProvider)}, switch${them} to ${targets}${each}`;
}

const offerTargets = (offer: ProviderSwitchOffer) => offer.toProvider.split("+").map(providerLabel).join(" and ");

export function ProviderSwitchCard({ offer, onAnswer, workerChoices, onWorkers, variant = "offer", showLine = true }: ProviderSwitchCardProps & { showLine?: boolean }) {
  /* One answer at a time: the first press wins, and every button waits for it. */
  const inflight = useRef(false);
  const [pending, setPending] = useState(false);
  const [always, setAlways] = useState(false);
  const once = (send: () => Promise<unknown>) => {
    if (inflight.current) return undefined;
    inflight.current = true;
    setPending(true);
    return Promise.resolve(send()).finally(() => {
      inflight.current = false;
      setPending(false);
    });
  };
  const answer = (accept: boolean) => once(() => onAnswer(accept, variant === "offer" && offer.kind === "switch" && accept && always ? true : undefined));
  const sessions = offer.sessions ?? [];
  const workers = offer.workers ?? [];
  return (
    <div className="flex flex-wrap items-center gap-1" data-testid="provider-switch" data-kind={offer.kind} data-variant={variant} data-sessions={Math.max(1, sessions.length)}>
      {showLine && <span className="mr-auto text-sm">{switchLine(offer, variant)}</span>}
      {(sessions.length > 1 || workers.length > 0) && (
        /* Who one answer reaches: every session on this limit, and the workers paused on it. */
        <div className="basis-full text-xs text-ink-3" data-testid="provider-switch-members">
          {sessions.length > 1 && <div data-testid="provider-switch-sessions">Sessions: {sessions.map((id, i) => {
            const target = offer.sessionTargets?.[id];
            return <span key={id}>{i > 0 && ", "}<span className="font-mono">{id}</span>{target && ` → ${providerLabel(target)}`}</span>;
          })}</div>}
          {/* Each worker as the engine words it: its replacement, and whether continuing bills per token. */}
          {workers.length > 0 && <div data-testid="provider-switch-workers">Workers waiting: {workers.map((w, i) => <span key={w}>{i > 0 && "; "}{w}</span>)}</div>}
        </div>
      )}
      {workers.length > 0 && onWorkers && (workerChoices ?? []).map((c) => (
        <LoadingButton key={c.id} type="button" className={`${quiet} px-s1 py-0.5 text-xs`} busy={pending} onClick={() => once(() => onWorkers(c.id))}>Workers: {c.label}</LoadingButton>
      ))}
      {variant === "offer" && offer.kind === "switch" && <label className="flex items-center gap-1 text-xs text-ink-3">
        <input type="checkbox" checked={always} disabled={pending} onChange={(e) => setAlways(e.target.checked)} />
        Always switch automatically
      </label>}
      <LoadingButton type="button" className={`${button} px-s1 py-0.5 text-xs`} busy={pending} onClick={() => answer(true)}>{variant === "notice" ? "Switch back now" : "Switch"}</LoadingButton>
      <LoadingButton type="button" className={`${quiet} px-s1 py-0.5 text-xs`} busy={pending} onClick={() => answer(false)}>{variant === "notice" ? "Dismiss" : "Not now"}</LoadingButton>
    </div>
  );
}

/* The transport the app's own client uses: the desktop shell (which adds the real URL and token), else plain fetch. */
export const daemonFetch = (): typeof fetch => (isTauri() ? tauriFetch : (i, o) => fetch(i, o));

/* Sends the answer along the item's own route (client.ts has no method for it, by arrangement). */
export async function answerProviderSwitch(client: Pick<DaemonClient, "baseUrl" | "token">, item: InboxItemDTO, accept: boolean, fetchImpl: typeof fetch = daemonFetch(), always?: boolean) {
  return post(client, item, { offer: item.request_id, accept, ...(accept && always ? { always: true } : {}) }, fetchImpl);
}

/* The workers' answer on the card (continue / wait / stop), for every worker it lists. */
export async function answerSwitchWorkers(client: Pick<DaemonClient, "baseUrl" | "token">, item: InboxItemDTO, choice: string, fetchImpl: typeof fetch = daemonFetch()) {
  return post(client, item, { offer: item.request_id, workers: choice }, fetchImpl);
}

async function post(client: Pick<DaemonClient, "baseUrl" | "token">, item: InboxItemDTO, body: object, fetchImpl: typeof fetch) {
  const route = item.action ?? { method: "POST", path: `/v1/sessions/${encodeURIComponent(item.root_id ?? "")}/provider-switch` };
  const response = await fetchImpl(`${client.baseUrl}${route.path}`, {
    method: route.method,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${client.token}`, Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => undefined)) as { error?: { message?: string } } | undefined;
    throw new Error(body?.error?.message ?? response.statusText);
  }
}
