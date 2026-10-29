/* The Account page's Usage section (design/usage/U2-account-usage.html):
   one card per provider brindle runs, each window it reports as a bar with
   its percent used and the clock time it resets, a one-model cap in small
   print, and what brindle does about it past 80% and 90%. A provider that
   refused work says when it is available again; one that reports nothing
   says why. A card's left edge turns warm past 80%, red past 90% or on a
   limit, green when a window resets within the hour; nothing moves. The
   numbers are the store's (store/usage.tsx, GET /usage). */

import { forwardRef } from "react";
import type { ProviderUsage, UsageWindow } from "../../api/client";
import { alert, card, h2, mono } from "../../components/ui";
import { useUsage } from "../../store/usage";
import { ago, caps, hint, level, noneText, nowSeconds, planWindows, providerCli, providerName, resetLine, resets, soon, clock, stateOf, windowLabel, type UsageState } from "./usage";

/* The card's left edge, per state. */
const EDGE: Record<UsageState, string> = { none: "", ok: "", warn: "before:bg-sap", stop: "before:bg-fail", limit: "before:bg-fail", soon: "before:bg-pass" };
const BAR: Record<ReturnType<typeof level>, string> = { ok: "bg-ink-3", warn: "bg-sap", stop: "bg-fail" };
const PCT: Record<ReturnType<typeof level>, string> = { ok: "text-ink", warn: "text-sap", stop: "text-fail" };
/* The provider's mark, in the palette's own colours. */
const DOT: Record<string, string> = { claude: "bg-sap", codex: "bg-ink-2", antigravity: "bg-branch", native: "bg-pass" };

function Meter({ w, now }: { w: UsageWindow; now: number }) {
  const l = level(w.used);
  const near = soon(w.resets_at, now);
  return (
    <div className="grid grid-cols-[1fr_auto] items-baseline gap-x-2.5 gap-y-0.5 text-[12.5px]" data-window={w.short} data-level={l}>
      <span className="text-ink-2">{windowLabel(w)}</span>
      <span className={`text-right font-mono text-[11.5px] ${PCT[l]}`} data-testid="usage-percent">
        {Math.round(w.used)}% used
      </span>
      <span className="col-span-2 h-[5px] overflow-hidden rounded-[3px] bg-canvas-2" role="meter" aria-label={windowLabel(w)} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(w.used)}>
        <i className={`block h-full rounded-[3px] ${BAR[l]}`} style={{ width: `${Math.min(100, Math.max(0, w.used))}%` }} />
      </span>
      <span className={`col-span-2 text-[11.5px] ${near ? "font-medium text-pass" : "text-ink-3"}`} data-testid="usage-reset">
        {resetLine(w, now)}
      </span>
    </div>
  );
}

function ProviderCard({ q, now }: { q: ProviderUsage; now: number }) {
  const state = stateOf(q, now);
  const plan = planWindows(q);
  const capped = caps(q);
  const note = q.known && !q.limited ? hint(q) : null;
  return (
    <article
      className={`relative flex min-h-24 flex-col gap-s1 border-t border-line py-2.5 pr-3 pl-[15px] before:absolute before:top-2.5 before:bottom-2.5 before:left-0 before:w-[2px] before:rounded-[2px] before:content-[''] ${EDGE[state]}`}
      aria-label={providerName(q.provider)}
      data-provider={q.provider}
      data-state={state}
    >
      <div className="flex items-center gap-[7px] text-[13px]">
        <i className={`inline-block size-2.5 flex-none rounded-[3px] ${DOT[q.provider] ?? "bg-ink-3"}`} aria-hidden />
        <b className="font-semibold">{providerName(q.provider)}</b>
        <span className="text-[11.5px] text-ink-3">{providerCli(q.provider)}</span>
        <span className="flex-1" />
        {q.known && q.updated_at != null && <span className="whitespace-nowrap text-[11px] text-ink-3">updated {ago(q.updated_at, now)}</span>}
      </div>
      {!q.known ? (
        <p className="m-0 text-xs leading-[1.45] text-ink-3">{noneText(q.provider)}</p>
      ) : q.limited ? (
        <div className="text-[12.5px]" data-testid="usage-limited">
          <b className="font-semibold text-fail">Limit reached</b>
          {q.limited_until != null && ` · available again ${resets(q.limited_until, now)}${soon(q.limited_until, now) ? ` (${clock(q.limited_until)})` : ""}`}
          <small className="mt-[3px] block text-[11.5px] leading-[1.4] text-ink-3">
            Workers on {providerName(q.provider)} wait for it; autopilot routes new work to the other providers until then.
          </small>
        </div>
      ) : (
        <>
          <div className="flex flex-col gap-s1">
            {plan.map((w) => (
              <Meter key={`${w.short}-${w.minutes}`} w={w} now={now} />
            ))}
          </div>
          {capped.length > 0 && (
            <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-ink-3" data-testid="usage-caps">
              {capped.map((w) => (
                <span key={w.name}>
                  {w.name} cap · {Math.round(w.used)}% used{w.resets_at != null && ` · resets ${resets(w.resets_at, now)}`}
                </span>
              ))}
            </div>
          )}
          {note && <p className="m-0 text-[11.5px] leading-[1.45] text-ink-2">{note}</p>}
        </>
      )}
    </article>
  );
}

/* The section; its ref is where Account › Usage scrolls to. */
export const UsageSection = forwardRef<HTMLElement>(function UsageSection(_props, ref) {
  const { usage, error } = useUsage();
  const now = nowSeconds();
  return (
    <section ref={ref} className={`${card} flex flex-col gap-2.5 px-s2 py-3.5`} aria-label="Usage" data-testid="usage">
      <div className="flex flex-wrap items-baseline gap-2.5">
        <h2 className={h2}>Usage</h2>
        <span className="max-w-[760px] text-xs leading-[1.45] text-ink-3">
          How much of each provider's subscription allowance is used and when it resets, as the provider's own CLI reports it: Claude Code's status line and <code className={`${mono} text-[11px]`}>/usage</code>, Codex's session log, Antigravity's limit errors. brindle never touches a sign-in or calls a provider for this.
        </span>
      </div>
      {error && !usage ? (
        <p role="alert" className={alert}>
          Could not load the usage: {error}
        </p>
      ) : usage ? (
        <div className="grid gap-2.5 md:grid-cols-2">
          {usage.providers.map((q) => (
            <ProviderCard key={q.provider} q={q} now={now} />
          ))}
        </div>
      ) : (
        <p className="text-sm text-ink-3">Loading…</p>
      )}
    </section>
  );
});
