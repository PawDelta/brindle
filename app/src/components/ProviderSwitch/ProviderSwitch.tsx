/* The Inbox card for brindle's offer to move an out-of-quota supervisor to
   another provider (kind "provider_switch"), or back once the original
   provider's limit resets (kind "provider_switch_back"). Switch and Not now
   answer POST /v1/sessions/{root}/provider-switch with {offer, accept}
   (brindle.provider_switch). Switch is a LoadingButton, so a second press
   while the first is out does nothing; the engine also answers a repeated
   Switch with the supervisor the first one started.

   DESIGN STUB: renders nothing yet; see docs/provider-switch-design.md. */

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

export function ProviderSwitchCard(_props: ProviderSwitchCardProps) {
  return null;
}
