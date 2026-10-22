/* What the Account screen works on, apart from React: the plans as the
   website's pricing page lists them (web/app/pricing/page.tsx is the source
   of truth: the four cards, their names, prices and "everything in X, plus"
   lists), and the links the site's own buttons open. Which features the
   person actually has is the engine's to say: a Pro route (GET /v1/cost)
   refusing with not_entitled is what locks a card here. Hosted learning is
   named only, never explained: the engine says whether it is on
   (account.learning) and what the learner kept as a note. */

/* The site's pages (web/lib/routes.ts: base path /brindle; the account dashboard at /account). */
export const PRICING_URL = "https://pawdelta.com/brindle/pricing/";
export const ACCOUNT_URL = "https://pawdelta.com/brindle/account";
export const ENTERPRISE_URL = "https://pawdelta.com/brindle/enterprise/";

export type Plan = "free" | "pro" | "team" | "enterprise";
export const PLANS: Plan[] = ["free", "pro", "team", "enterprise"];

/* The site's docs (web/app/docs, guides, enterprise; getting started), where a plan's feature is explained. */
export const DOCS_URL = "https://pawdelta.com/brindle/docs/";
export const GETTING_STARTED_URL = "https://pawdelta.com/brindle/getting-started/";

/* Where a paying customer uses a feature: a screen of the app, the Spend
   card on the Account screen, or a page of the site (docs, the account
   dashboard, the enterprise page) in the system browser. */
export type FeatureUse = { screen: "agents" | "settings" | "spend" } | { url: string };

/* One line of a plan's card, the entitlement feature (pro/license.py
   features) that turns it on (none for what every brindle has), and where
   to use or learn it. */
export interface TierFeature {
  what: string;
  key?: string;
  use?: FeatureUse;
}

/* A plan's card on the pricing page. */
export interface Tier {
  plan: Plan;
  /* The card's name, and the tier badge beside it. */
  name: string;
  label: string;
  price: string;
  audience: string;
  /* The list's lead ("All of brindle", "Everything in Pro, plus"). */
  lead: string;
  features: TierFeature[];
}

export const TIERS: Tier[] = [
  {
    plan: "free",
    name: "Developer Core",
    label: "Free",
    price: "$0 forever",
    audience: "No account needed.",
    lead: "All of brindle",
    features: [
      { what: "Parallel agents and autopilot" },
      { what: "Merges only after review and checks pass" },
      { what: "Claude Code, Codex, Antigravity and open-weight models" },
      { what: "No limit on agents, workers or sessions" },
      { what: "Code goes only to the providers you pick" },
    ],
  },
  {
    plan: "pro",
    name: "Power-Dev Pro",
    label: "Pro",
    price: "$20 / month",
    audience: "One person running many agents on real backends.",
    lead: "Everything in Developer Core, plus",
    features: [
      { what: "Multi-repo sessions", key: "multi_repo", use: { url: `${DOCS_URL}commands/#paid-commands` } },
      { what: "A coordinator that holds and orders queued work across sessions", key: "coordinator", use: { url: `${DOCS_URL}commands/#paid-commands` } },
      { what: "Per-worktree databases", key: "services", use: { url: `${DOCS_URL}config/` } },
      { what: "Hosted learning", key: "learning", use: { screen: "agents" } },
      { what: "Settings sync", key: "settings_sync", use: { screen: "settings" } },
      { what: "Federated cloud credentials", key: "federation", use: { url: `${DOCS_URL}commands/#paid-commands` } },
      { what: "brindle CI on 1 repo", key: "ci_fix", use: { url: `${DOCS_URL}commands/#ci` } },
    ],
  },
  {
    plan: "team",
    name: "DevOps Automation",
    label: "Team",
    price: "$40 / seat / month",
    audience: "Teams running agents on shared code and in their pipelines.",
    lead: "Everything in Pro, plus",
    features: [
      { what: "brindle CI on every repo", key: "ci", use: { url: `${DOCS_URL}commands/#ci` } },
      { what: "Org policy and required review", key: "team", use: { screen: "agents" } },
      { what: "Org-wide federation", key: "federation", use: { url: `${DOCS_URL}commands/#paid-commands` } },
      { what: "Budgets, alerts and throttles", key: "org_budgets", use: { screen: "spend" } },
      { what: "Members, roles and seats", key: "team", use: { url: ACCOUNT_URL } },
      { what: "Audit feed", key: "team", use: { url: ACCOUNT_URL } },
    ],
  },
  {
    plan: "enterprise",
    name: "Enterprise Sovereign",
    label: "Enterprise",
    price: "$60 / seat / month",
    audience: "Defense, finance, healthcare and government teams.",
    lead: "Everything in Team, plus",
    features: [
      { what: "Air-gap mode", key: "airgap", use: { url: `${ENTERPRISE_URL}#adds` } },
      { what: "Signed audit trail", key: "audit", use: { url: `${ENTERPRISE_URL}#adds` } },
      { what: "Remote shutdown and throttle", key: "managed_rollout", use: { url: `${ENTERPRISE_URL}#rollout` } },
      { what: "Custom roles and team budgets", key: "cost_centers", use: { url: `${PRICING_URL}#limits` } },
      { what: "Spend by member", key: "cost_centers", use: { url: ACCOUNT_URL } },
    ],
  },
];

/* Where a plan's people get the most from it: the site's page for the plan. */
export const PLAN_GUIDE_URL: Record<Plan, string> = {
  free: `${DOCS_URL}recommended-use/`,
  pro: `${DOCS_URL}recommended-use/`,
  team: `${GETTING_STARTED_URL}team/`,
  enterprise: ENTERPRISE_URL,
};

/* Whether the entitlement turns a card's line on: what every brindle has
   always; the rest only when the engine lists its feature. */
export const entitled = (f: TierFeature, features: string[] | undefined): boolean => !f.key || (features ?? []).includes(f.key);

/* The plan a name from the entitlement means (anything unknown reads as free). */
export const planOf = (plan: string | null | undefined): Plan => (PLANS.includes(plan as Plan) ? (plan as Plan) : "free");

export const tierOf = (plan: Plan): Tier => TIERS.find((t) => t.plan === plan)!;

/* Whether a plan's column has a tier's additions: each plan has the tiers below it. */
export const planHas = (plan: Plan, tier: Tier): boolean => PLANS.indexOf(plan) >= PLANS.indexOf(tier.plan);

/* A paying customer: the app sells them nothing, bar one quiet link up a tier. */
export const paid = (plan: Plan): boolean => plan !== "free";

/* The tier above a plan, for that link: Pro → Team, Team → Enterprise; none above Enterprise. */
export const nextPlan = (plan: Plan): Plan | null => (plan === "pro" ? "team" : plan === "team" ? "enterprise" : null);

/* Where moving up to a plan happens: the account page for Team, the enterprise page for Enterprise. */
export const UPGRADE_URL: Record<Plan, string> = { free: ACCOUNT_URL, pro: ACCOUNT_URL, team: ACCOUNT_URL, enterprise: ENTERPRISE_URL };

export const PLAN_LABEL: Record<Plan, string> = { free: "Free", pro: "Pro", team: "Team", enterprise: "Enterprise" };

/* A date as the CLI prints it (_when). */
export const when = (ts: number | null | undefined): string => (ts ? new Date(ts * 1000).toISOString().slice(0, 10) : "–");

/* Dollars as the cost report prints them. */
export const dollars = (n: number): string => (n >= 100 ? `$${Math.round(n)}` : `$${n.toFixed(2)}`);

export function tokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${Math.round(n / 1_000)}k`;
  return String(n);
}
