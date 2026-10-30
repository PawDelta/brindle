/* Account: the person's brindle plan (GET /v1/account) and what brindle is
   spending (GET /v1/cost). Sign-in runs on the daemon as a job
   (POST /v1/account/login): it starts the loopback callback and publishes
   the authorize URL, which the app opens in the system browser, never a
   webview, so the person's own browser session signs in; a device code is
   the fallback for a machine with no browser. A paying customer sees their
   plan as the pricing page describes it and the billing page; nothing is
   sold to them. Someone on the free plan, or signed out, sees the pricing
   page's plans compared and the site's own way to upgrade; a paying
   customer below Enterprise gets one quiet link up a tier in the plan
   card's header, and no comparison or banner. Nothing here
   decides what is allowed: a Pro feature's route refuses with not_entitled
   when the plan lacks it, and that refusal locks its card. Switching org
   (POST /v1/account/switch-org) takes the org id, as the CLI does. Under
   the plan and spend, Usage (./UsageSection.tsx, GET /v1/usage): each provider's
   subscription windows, where the sidebar's reading near a limit leads. */

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { DaemonError, type AccountInfo, type Cost, type DaemonClient } from "../../api/client";
import type { OpenUrl } from "../../api/open";
import { plain } from "../../api/wording";
import { PageHeader } from "../../components/PageHeader";
import { RepoSelect, useRepo } from "../../components/RepoSelect";
import { alert, button, card, chip, field, h2, label, main, mono, notice, pill, quiet } from "../../components/ui";
import { useSessions } from "../../store/sessions";
import { useUsage } from "../../store/usage";
import { useAppState, useStore } from "../../store/useStore";
import { useJob } from "../Branch/useJob";
import { UsageSection } from "./UsageSection";
import { ACCOUNT_URL, PLANS, PLAN_GUIDE_URL, PLAN_LABEL, PRICING_URL, TIERS, UPGRADE_URL, dollars, entitled, nextPlan, paid, planHas, planOf, tierOf, tokens, when, type FeatureUse, type Plan, type TierFeature } from "./account";

export interface AccountScreenProps {
  client: DaemonClient;
  /* Opens a page in the system browser (api/open.ts openExternal). */
  openUrl: OpenUrl;
  /* Opens a screen of the app, where a plan's feature is used (Agents, Settings). */
  onNavigate?(screen: "agents" | "settings"): void;
  /* The section to scroll to on opening: "usage" from the sidebar's reading near a limit. */
  section?: "usage";
  /* A new value asks to scroll to `section` again, the page already open. */
  ask?: number;
}

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));
const DAYS = [7, 30, 90];

/* Signing in, as a job: the URL to open arrives on the bus; done means the daemon stored the tokens. */
function SignIn({ client, openUrl, onDone }: { client: DaemonClient; openUrl: OpenUrl; onDone(): void }) {
  const { job, busy, start, clear } = useJob();
  const [url, setUrl] = useState<string | null>(null);
  const [openError, setOpenError] = useState<string | null>(null);
  const opened = useRef<string | null>(null);

  // The authorize URL is the progress line that is one; open it once.
  const link = job?.progress.find((line) => /^https?:\/\//.test(line)) ?? null;
  useEffect(() => {
    if (!link || opened.current === link || job?.status !== "running") return;
    opened.current = link;
    setUrl(link);
    openUrl(link).catch((e: unknown) => setOpenError(message(e)));
  }, [link, job?.status, openUrl]);

  useEffect(() => {
    if (job?.status === "done") onDone();
  }, [job?.status, onDone]);

  const begin = (device: boolean) => {
    setUrl(null);
    setOpenError(null);
    opened.current = null;
    void start(device ? "Device code sign-in" : "Browser sign-in", () => client.accountLogin(device));
  };

  const lines = job?.progress.filter((line) => line !== link) ?? [];
  return (
    <section className={`${card} flex flex-col gap-s1 px-s2 py-s2`} aria-label="Sign in" data-testid="sign-in">
      <h2 className={h2}>Not signed in</h2>
      <p className="text-sm text-ink-2">brindle is complete without an account. A brindle Pro plan adds hosted learning, cost reports, Docker services per worktree and more, listed below.</p>
      {!busy && (
        <div className="flex flex-wrap items-center gap-s1">
          <button type="button" className={main} onClick={() => begin(false)}>
            Sign in with your browser
          </button>
          <button type="button" className={quiet} onClick={() => begin(true)}>
            Use a device code instead
          </button>
        </div>
      )}
      {job && (
        <div className="flex flex-col gap-1 rounded-md bg-canvas-2 px-s2 py-s1 text-sm" role="status" data-testid="sign-in-state">
          {job.status === "starting" && <span>Starting…</span>}
          {job.status === "running" && !url && lines.length === 0 && <span>Waiting for the daemon…</span>}
          {job.status === "running" && url && (
            <>
              <span>Finish signing in in your browser; this page follows along.</span>
              <span className="flex flex-wrap items-center gap-s1 text-xs text-ink-3">
                <span className={`${mono} min-w-0 break-all`}>{url}</span>
                <button type="button" className={quiet} onClick={() => void openUrl(url).catch((e: unknown) => setOpenError(message(e)))}>
                  Open again
                </button>
              </span>
              {openError && <span className="text-sap">The browser didn't open ({openError}): open the address above yourself.</span>}
            </>
          )}
          {/* The device flow's lines are the code and where to type it: kept as they are. */}
          {lines.map((line, i) => (
            <span key={i} className={line.startsWith("Open ") || /code/i.test(line) ? "font-medium" : "text-ink-2"}>
              {line}
            </span>
          ))}
          {job.status === "failed" && (
            <span role="alert" className="text-sap">
              Sign-in failed: {job.error}
            </span>
          )}
          {job.status === "failed" && (
            <button type="button" className={`${quiet} self-start`} onClick={clear}>
              Dismiss
            </button>
          )}
        </div>
      )}
    </section>
  );
}

function PlanCard({ account, client, openUrl, onChanged }: { account: AccountInfo; client: DaemonClient; openUrl: OpenUrl; onChanged(): void }) {
  const plan = planOf(account.plan);
  const [org, setOrg] = useState("");
  const orgs = account.orgs ?? [];
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [openError, setOpenError] = useState<string | null>(null);

  const run = async (what: () => Promise<{ messages: string[] }>) => {
    setBusy(true);
    setError(null);
    try {
      const got = await what();
      setSaid(got.messages);
      onChanged();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };

  const switchOrg = (e: FormEvent) => {
    e.preventDefault();
    if (org.trim()) void run(() => client.switchOrg(org.trim()));
  };

  return (
    <section className={`${card} flex flex-col gap-s1 px-s2 py-s2`} aria-label="Plan" data-testid="plan">
      <header className="flex flex-wrap items-center gap-s1">
        <h2 className={h2}>{account.name ?? "Signed in"}</h2>
        <span className={`${pill} text-ink-2`} data-testid="plan-pill">
          brindle {PLAN_LABEL[plan]}
        </span>
        <span className="flex-1" />
        <button type="button" className={button} onClick={() => void openUrl(ACCOUNT_URL).catch((e: unknown) => setOpenError(message(e)))} data-testid="manage-billing">
          Manage billing &amp; members ↗
        </button>
        <button type="button" className={quiet} disabled={busy} onClick={() => void run(() => client.accountLogout())}>
          Sign out
        </button>
      </header>
      {openError && (
        <p role="alert" className={alert}>
          The browser didn't open ({openError}): the account page is {ACCOUNT_URL}.
        </p>
      )}
      <dl className="grid grid-cols-[auto_1fr] gap-x-s2 gap-y-0.5 text-sm" data-testid="entitlement">
        {account.org_id && (
          <>
            <dt className="text-ink-3">org</dt>
            <dd className={mono}>
              {account.org_id}
              {account.role && <span className="font-sans text-ink-2"> ({account.role})</span>}
            </dd>
          </>
        )}
        {account.status && (
          <>
            <dt className="text-ink-3">status</dt>
            <dd>
              {account.in_grace ? `offline grace until ${when(account.grace_until)}: reconnect to refresh` : account.status}
              {account.seats != null && ` · ${account.seats} ${account.seats === 1 ? "seat" : "seats"}`}
            </dd>
          </>
        )}
        {account.expires_at != null && (
          <>
            <dt className="text-ink-3">entitlement</dt>
            <dd>renews by {when(account.expires_at)}</dd>
          </>
        )}
        {account.learning != null && (
          <>
            <dt className="text-ink-3">hosted learning</dt>
            <dd data-testid="learning">
              {account.learning ? "on" : "off"}
              {account.learning_note && <span className="block text-xs text-ink-2">{account.learning_note}</span>}
            </dd>
          </>
        )}
      </dl>
      <form className="flex flex-col gap-1" aria-label="Switch org" onSubmit={switchOrg}>
        <span className={label}>Work under another org</span>
        <div className="flex flex-wrap items-center gap-s1">
          {orgs.length > 0 ? (
            // The orgs the account belongs to, as the engine lists them (GET /account orgs).
            <select className={field} aria-label="Org" value={org || (orgs.find((o) => o.current)?.id ?? "")} disabled={busy} onChange={(e) => setOrg(e.target.value)} data-testid="org-picker">
              {orgs.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name || o.id} · {o.personal ? "personal" : o.plan}
                  {o.role ? ` · ${o.role}` : ""}
                  {o.current ? " (current)" : ""}
                </option>
              ))}
            </select>
          ) : (
            <input className={`${field} font-mono`} aria-label="Org id" placeholder="org id" value={org} disabled={busy} onChange={(e) => setOrg(e.target.value)} />
          )}
          <button type="submit" className={button} disabled={busy || !org.trim() || org === orgs.find((o) => o.current)?.id}>
            Switch org
          </button>
        </div>
        <span className="text-xs text-ink-3">The plan and policies follow the org you work under.{orgs.length === 0 && " The engine couldn't list your orgs: give the id."}</span>
      </form>
      {said.length > 0 && (
        <ul role="status" className={notice}>
          {said.map((line, i) => (
            <li key={i}>{plain(line)}</li>
          ))}
        </ul>
      )}
      {error && (
        <p role="alert" className={alert}>
          {error}
        </p>
      )}
    </section>
  );
}

/* Where the person goes to use a feature: a screen of the app, the Spend
   card, or a page of the site in the system browser. */
export type UseFeature = (use: FeatureUse) => void;

/* One line of a plan's card: on when the entitlement has its feature, else
   quietly unavailable (a lapsed or partial account), with nothing to buy.
   A line that is on is a link to where the feature is used or explained. */
function PlanLine({ f, features, dim = false, onUse }: { f: TierFeature; features: string[] | undefined; dim?: boolean; onUse: UseFeature }) {
  const on = entitled(f, features);
  const use = on ? f.use : undefined;
  return (
    <li className={`flex items-baseline gap-s1 ${on ? (dim ? "text-ink-2" : "") : "text-ink-3"}`} data-available={on} data-feature={f.key ?? "core"}>
      <span aria-hidden className={`w-4 shrink-0 ${on ? "text-pass" : "text-ink-3"}`}>
        {on ? "✓" : "–"}
      </span>
      <span>
        {use ? (
          <button type="button" className="text-left underline decoration-line-strong underline-offset-2 hover:text-ink" onClick={() => onUse(use)} data-use={"screen" in use ? use.screen : "site"}>
            {f.what}
            {"url" in use && <span className="text-ink-3"> ↗</span>}
          </button>
        ) : (
          f.what
        )}
        {!on && <span className="text-xs"> · not in your entitlement</span>}
      </span>
    </li>
  );
}

/* A paying customer's plan: its card as the pricing page has it (what it
   adds, with the tiers below behind a disclosure), each line as the
   entitlement actually has it and a link to where it is used. Nothing is
   sold here, bar one quiet link up a tier, and the site's page on getting
   the most from the plan. */
function PaidPlan({ plan, features, openUrl, onUse }: { plan: Plan; features: string[] | undefined; openUrl: OpenUrl; onUse: UseFeature }) {
  const tier = tierOf(plan);
  const next = nextPlan(plan);
  const [below, setBelow] = useState(false);
  const included = TIERS.filter((t) => PLANS.indexOf(t.plan) < PLANS.indexOf(plan)).reverse();
  const missing = [tier, ...included].flatMap((t) => t.features).filter((f) => !entitled(f, features)).length;
  const open = (url: string) => void openUrl(url).catch(() => undefined);
  return (
    <section className={`${card} flex flex-col gap-s1 px-s2 py-s2`} aria-label="Your plan" data-testid="features">
      <header className="flex flex-wrap items-baseline gap-s1">
        <h2 className={h2}>{tier.name}</h2>
        <span className={`${pill} text-ink-2`}>{tier.label}</span>
        <span className="flex-1" />
        <button type="button" className={`${quiet} text-xs`} onClick={() => open(PLAN_GUIDE_URL[plan])} data-testid="plan-guide">
          Get the most from {PLAN_LABEL[plan]} ↗
        </button>
        {/* A paying customer may still move up a tier, quietly: a text link, no comparison, no banner. */}
        {next && (
          <button type="button" className={`${quiet} text-xs`} onClick={() => open(UPGRADE_URL[next])} data-testid="upgrade-next">
            Upgrade to {PLAN_LABEL[next]} ↗
          </button>
        )}
      </header>
      <p className="text-sm text-ink-2" data-testid="features-summary">
        {tier.audience} {tier.lead}:
      </p>
      <ul className="grid gap-x-s2 gap-y-0.5 text-sm md:grid-cols-2" data-testid="plan-features">
        {tier.features.map((f) => (
          <PlanLine key={f.what} f={f} features={features} onUse={onUse} />
        ))}
      </ul>
      {missing > 0 && (
        <p className="text-xs text-ink-3" data-testid="unavailable-note">
          {missing === 1 ? "One line isn't in your entitlement right now" : `${missing} lines aren't in your entitlement right now`}; the account page shows why.
        </p>
      )}
      {included.length > 0 && (
        <div className="flex flex-col gap-1">
          <button type="button" className={`${quiet} self-start`} aria-expanded={below} onClick={() => setBelow((s) => !s)}>
            {below ? "Hide the rest" : `See everything your plan includes (${included.map((t) => t.label).join(" and ")} too)`}
          </button>
          {below &&
            included.map((t) => (
              <div key={t.plan} className="flex flex-col gap-0.5" data-tier={t.plan}>
                <div className={label}>
                  {t.name} · {t.lead}
                </div>
                <ul className="grid gap-x-s2 gap-y-0.5 text-sm md:grid-cols-2">
                  {t.features.map((f) => (
                    <PlanLine key={f.what} f={f} features={features} dim onUse={onUse} />
                  ))}
                </ul>
              </div>
            ))}
        </div>
      )}
    </section>
  );
}

/* The plans for someone on the free one (signed in or not): the free plan
   in a sentence, the pricing page's four cards as columns over its lists
   (theirs highlighted), and the site's own way to upgrade. */
function Plans({ openUrl, signedIn }: { openUrl: OpenUrl; signedIn: boolean }) {
  const [openError, setOpenError] = useState<string | null>(null);
  const open = (url: string) => void openUrl(url).catch((e: unknown) => setOpenError(message(e)));
  const free = tierOf("free");
  return (
    <section className={`${card} flex flex-col gap-s2 px-s2 py-s2`} aria-label="Plans" data-testid="features">
      <header className="flex flex-wrap items-baseline gap-s1">
        <h2 className={h2}>Your plan</h2>
        <span className="flex-1" />
        <button type="button" className={button} onClick={() => open(PRICING_URL)} data-testid="pricing">
          Pricing ↗
        </button>
        <button type="button" className={signedIn ? main : button} onClick={() => open(ACCOUNT_URL)} data-testid="upgrade">
          Upgrade to Pro ↗
        </button>
      </header>
      <p className="text-sm" data-testid="features-summary">
        <span className="font-medium">{free.name}</span>
        <span className="text-ink-2">, {free.price.toLowerCase()}: {free.lead.toLowerCase()}. Paid plans add per-worktree services, multi-repo sessions, and the controls and records teams need.</span>
      </p>
      <table className="w-full text-sm" aria-label="Plans compared" data-testid="plans">
        <thead>
          <tr>
            <th className="py-1 text-left font-normal text-ink-3" />
            {TIERS.map((t) => (
              <th key={t.plan} className={`w-24 py-1 text-center ${t.plan === "free" ? "rounded-t-md bg-pass-soft text-pass" : "font-medium text-ink-2"}`} data-plan={t.plan} data-current={t.plan === "free" || undefined}>
                {t.label}
                <span className="block text-xs font-normal text-ink-3">{t.price}</span>
                {t.plan === "free" && <span className="block text-xs font-normal">yours</span>}
              </th>
            ))}
          </tr>
        </thead>
        {TIERS.map((tier) => (
          <tbody key={tier.plan} data-group={tier.plan}>
            <tr className="border-t border-line">
              <th colSpan={5} className={`${label} py-1 text-left`}>
                {tier.name} · {tier.lead}
              </th>
            </tr>
            {tier.features.map((f) => (
              <tr key={f.what} className="border-t border-line">
                <td className="py-1 pr-s2 text-ink-2">{f.what}</td>
                {TIERS.map((t) => (
                  <td key={t.plan} className={`py-1 text-center ${t.plan === "free" ? "bg-pass-soft" : ""}`} aria-label={planHas(t.plan, tier) ? `${t.label}: included` : `${t.label}: not included`}>
                    <span className={planHas(t.plan, tier) ? "text-pass" : "text-ink-3"}>{planHas(t.plan, tier) ? "✓" : "–"}</span>
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        ))}
      </table>
      <p className="text-xs text-ink-3">The pricing page has the full comparison and every cap. Enterprise contracts: support@pawdelta.com.</p>
      {openError && (
        <p role="alert" className={alert}>
          The browser didn't open ({openError}): plans are at {PRICING_URL}.
        </p>
      )}
    </section>
  );
}

/* What brindle spent, at list prices (GET /v1/cost); locked by the engine
   without the cost feature. `revision` counts the account's reloads: a
   sign-in, a sign-out or an org switch can change what the engine allows,
   so the report is asked for again after each. */
function Spend({ client, openUrl, signedIn, paying, revision }: { client: DaemonClient; openUrl: OpenUrl; signedIn: boolean; paying: boolean; revision: number }) {
  const pick = useRepo();
  const { repo } = pick;
  const [days, setDays] = useState(30);
  const [cost, setCost] = useState<Cost | null>(null);
  const [locked, setLocked] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  useEffect(() => {
    const mine = ++seq.current;
    setCost(null);
    setLocked(null);
    setError(null);
    client.cost(repo ?? undefined, days).then(
      (c) => mine === seq.current && setCost(c),
      (e: unknown) => {
        if (mine !== seq.current) return;
        if (e instanceof DaemonError && e.code === "not_entitled") setLocked(e.message);
        else setError(message(e));
      },
    );
  }, [client, repo, days, signedIn, revision]);

  const models = cost ? Object.entries(cost.by_model).sort((a, b) => b[1].dollars - a[1].dollars) : [];
  return (
    <section className={`${card} flex flex-col gap-s1 px-s2 py-s2`} aria-label="Spend" data-testid="spend">
      <header className="flex flex-wrap items-baseline gap-s1">
        <h2 className={h2}>Spend</h2>
        <span className="text-xs text-ink-3">At list prices; a subscription or discount makes the real bill lower.</span>
        <span className="flex-1" />
        <RepoSelect {...pick} disabled={locked !== null} />
      </header>
      {locked ? (
        <div className="flex flex-col gap-s1 rounded-md bg-canvas-2 px-s2 py-s2 text-sm" data-testid="spend-locked">
          <span>
            <span aria-hidden>🔒 </span>
            {paying ? `The engine refuses the cost report for this plan: ${plain(locked)}` : "Cost reports come with Pro."}
          </span>
          {/* Nothing is sold to a paying customer: the engine's word stands as it is. */}
          {!paying && (
            <button type="button" className={`${button} self-start`} onClick={() => void openUrl(PRICING_URL).catch(() => undefined)} data-testid="spend-pricing">
              See the plans ↗
            </button>
          )}
        </div>
      ) : error ? (
        <p role="alert" className={alert}>
          Could not load the cost report: {error}
        </p>
      ) : cost ? (
        <>
          <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Days">
            {DAYS.map((d) => (
              <button key={d} type="button" className={`${chip} border ${days === d ? "border-pass/55 bg-pass-soft text-pass" : "border-line text-ink-2 hover:bg-surface-2"}`} aria-pressed={days === d} onClick={() => setDays(d)}>
                {d} days
              </button>
            ))}
          </div>
          <p className="text-2xl font-semibold tracking-tight" data-testid="spend-total">
            {dollars(cost.total.dollars)}
            <span className="ml-s1 text-sm font-normal text-ink-3">
              {tokens(cost.total.tokens)} tokens in {cost.days} days
              {cost.total.free > 0 && `, ${tokens(cost.total.free)} on free models`}
              {cost.total.unknown > 0 && `, ${tokens(cost.total.unknown)} at an unknown price`}
            </span>
          </p>
          {models.length === 0 ? (
            <p className="text-sm text-ink-2">Nothing recorded in this window.</p>
          ) : (
            <table className="text-sm">
              <thead>
                <tr className={`${label} text-left`}>
                  <th className="py-0.5 font-medium">model</th>
                  <th className="py-0.5 text-right font-medium">tokens</th>
                  <th className="py-0.5 text-right font-medium">cost</th>
                </tr>
              </thead>
              <tbody>
                {models.map(([model, b]) => (
                  <tr key={model} data-model={model}>
                    <td className={`${mono} py-0.5`}>{model}</td>
                    <td className="py-0.5 text-right text-ink-2">{tokens(b.tokens)}</td>
                    <td className="py-0.5 text-right">{dollars(b.dollars)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      ) : (
        <p className="text-sm text-ink-3">Loading…</p>
      )}
    </section>
  );
}

export function AccountScreen({ client, openUrl, onNavigate, section, ask }: AccountScreenProps) {
  const store = useStore();
  const spend = useRef<HTMLDivElement>(null);
  const usageRef = useRef<HTMLElement>(null);
  const { reload: reloadUsage } = useUsage();
  // The page shows the usage as it is now, not as of the last minute's poll.
  useEffect(() => {
    void reloadUsage();
  }, [reloadUsage]);
  const [account, setAccount] = useState<AccountInfo | null>(null);
  // Again once the plan's cards above it have loaded and moved it down.
  const placed = account !== null;
  useEffect(() => {
    if (section === "usage") usageRef.current?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }, [section, ask, placed]);
  /* Where a plan's feature is used: a screen, the Spend card, or the site. */
  const use: UseFeature = (where) => {
    if ("url" in where) void openUrl(where.url).catch(() => undefined);
    else if (where.screen === "spend") spend.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    else onNavigate?.(where.screen);
  };
  /* How many times the account was (re)loaded: what the engine allows may have changed each time. */
  const [revision, setRevision] = useState(0);
  const [loadError, setLoadError] = useState<string | null>(null);
  const bar = useAppState((s) => s.org.bar);
  const connection = useAppState((s) => s.connection);
  const { repos } = useSessions();
  // The learner's kept note is per repo: the first repo's, as the Spend card starts on it.
  const repo = repos?.[0]?.repo_root;

  const load = useCallback(
    () =>
      client.account(repo).then(
        (a) => {
          setAccount(a);
          store.dispatch({ type: "account", account: a });
          setRevision((n) => n + 1);
          setLoadError(null);
        },
        (e: unknown) => setLoadError(message(e)),
      ),
    [client, repo, store],
  );
  // On start, and again when the stream (re)opens: a sign-in from the CLI changes it too.
  useEffect(() => {
    if (connection === "open" || connection === "idle") void load();
  }, [load, connection]);

  const plan = planOf(account?.plan);
  const paying = account?.signed_in === true && paid(plan);
  const lede = !account
    ? "Your plan and what brindle is spending."
    : !account.signed_in
      ? "Sign in to see your plan and what brindle is spending; brindle works without one."
      : paying
        ? `Your brindle ${PLAN_LABEL[plan]} plan: what it includes, and what brindle is spending.`
        : "You're on the free plan: what it includes, what the paid plans add, and what brindle is spending.";
  return (
    <section className="flex flex-col gap-s2" aria-label="Account">
      <PageHeader title="Account" lede={lede} />
      {bar && bar.length > 0 && (
        <p className={notice} data-testid="org-bar">
          {bar.join(" · ")}
        </p>
      )}
      {loadError && (
        <div role="alert" className={`${alert} flex items-center justify-between gap-s2`}>
          <span>Could not load the account: {loadError}</span>
          <button type="button" className={button} onClick={() => void load()}>
            Retry
          </button>
        </div>
      )}
      {account && (
        <>
          <div className="grid gap-s2 lg:grid-cols-[3fr_2fr] lg:items-start">
            {account.signed_in ? (
              <PlanCard account={account} client={client} openUrl={openUrl} onChanged={() => void load()} />
            ) : (
              <SignIn client={client} openUrl={openUrl} onDone={() => void load()} />
            )}
            <div ref={spend} className="contents">
              <Spend client={client} openUrl={openUrl} signedIn={account.signed_in} paying={paying} revision={revision} />
            </div>
          </div>
        </>
      )}
      {/* Usage is the providers' own allowance, signed in or not. */}
      <UsageSection ref={usageRef} />
      {account && (paying ? <PaidPlan plan={plan} features={account.features} openUrl={openUrl} onUse={use} /> : <Plans openUrl={openUrl} signedIn={account.signed_in} />)}
    </section>
  );
}
