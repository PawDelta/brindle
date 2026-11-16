/* The Inbox card for brindle's offer to move an out-of-quota supervisor to
   another provider (kind "provider_switch"), or back once the original
   provider's limit resets (kind "provider_switch_back"). The daemon sends it
   as an ordinary InboxItemDTO (docs/provider-switch-design.md, decision 9):
   request_id is the offer id, detail reads "from → to", and action is
   POST /v1/sessions/{root}/provider-switch, which takes {offer, accept}.
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
}

export interface ProviderSwitchCardProps {
  offer: ProviderSwitchOffer;
  /* The person's answer: true for Switch, false for Not now. */
  onAnswer(accept: boolean): Promise<unknown>;
}

export const PROVIDER_SWITCH_KINDS = ["provider_switch", "provider_switch_back"];

/* The offer an Inbox item carries, from its existing fields. */
export function offerOf(item: InboxItemDTO): ProviderSwitchOffer {
  const [from, to] = (item.detail ?? "").split(/\s*→\s*/);
  return {
    offerId: item.request_id ?? "",
    rootId: item.root_id ?? "",
    kind: item.kind === "provider_switch_back" ? "back" : "switch",
    fromProvider: from ?? "",
    toProvider: to ?? "",
    limitedUntil: null,
  };
}

/* The line the card leads with. A back offer names the provider with quota again (the one it returns to). */
export function switchLine(offer: ProviderSwitchOffer): string {
  return offer.kind === "back"
    ? `${providerLabel(offer.toProvider)} has quota again, switch back`
    : `Out of quota on ${providerLabel(offer.fromProvider)}, switch to ${providerLabel(offer.toProvider)}`;
}

export function ProviderSwitchCard({ offer, onAnswer, showLine = true }: ProviderSwitchCardProps & { showLine?: boolean }) {
  /* One answer per offer: the first press wins, and both buttons wait for it. */
  const inflight = useRef(false);
  const [pending, setPending] = useState(false);
  const answer = (accept: boolean) => {
    if (inflight.current) return undefined;
    inflight.current = true;
    setPending(true);
    return Promise.resolve(onAnswer(accept)).finally(() => {
      inflight.current = false;
      setPending(false);
    });
  };
  return (
    <div className="flex flex-wrap items-center gap-1" data-testid="provider-switch" data-kind={offer.kind}>
      {showLine && <span className="mr-auto text-sm">{switchLine(offer)}</span>}
      <LoadingButton type="button" className={`${button} px-s1 py-0.5 text-xs`} busy={pending} onClick={() => answer(true)}>Switch</LoadingButton>
      <LoadingButton type="button" className={`${quiet} px-s1 py-0.5 text-xs`} busy={pending} onClick={() => answer(false)}>Not now</LoadingButton>
    </div>
  );
}

/* The transport the app's own client uses: the desktop shell (which adds the real URL and token), else plain fetch. */
export const daemonFetch = (): typeof fetch => (isTauri() ? tauriFetch : (i, o) => fetch(i, o));

/* Sends the answer along the item's own route (client.ts has no method for it, by arrangement). */
export async function answerProviderSwitch(client: Pick<DaemonClient, "baseUrl" | "token">, item: InboxItemDTO, accept: boolean, fetchImpl: typeof fetch = daemonFetch()) {
  const route = item.action ?? { method: "POST", path: `/v1/sessions/${encodeURIComponent(item.root_id ?? "")}/provider-switch` };
  const response = await fetchImpl(`${client.baseUrl}${route.path}`, {
    method: route.method,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${client.token}`, Accept: "application/json" },
    body: JSON.stringify({ offer: item.request_id, accept }),
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => undefined)) as { error?: { message?: string } } | undefined;
    throw new Error(body?.error?.message ?? response.statusText);
  }
}
