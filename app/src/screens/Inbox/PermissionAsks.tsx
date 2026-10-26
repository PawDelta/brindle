/* The Inbox's permission items (design/permissions-preview.html, its Inbox
   page): the requests brindle's hook holds, generalized and grouped by the
   daemon (GET /permissions/asks), one card per kind and general form. A card
   says what it was generalized from and lists every request in it; its
   scope defaults to the general form (Narrow to this request picks the
   exact one), and one decision resolves the item (POST
   /permissions/asks/decide): Allow once, Allow for a duration or this
   session, Always allow, Deny, Always deny, as the item's rule permits.
   Above the cards, the stall notice: how many workers wait and on what,
   with Allow all / Always allow (POST /permissions/asks/allow-all, counted
   by GET /permissions/asks/plan) and, for requests a deny rule blocks, the
   exception that unblocks them (POST /permissions/asks/unblock) and Open
   the deny rule (Settings › Permissions with that rule's editor). The
   cards of /inbox for the same held requests are left to these. */

import { useCallback, useEffect, useMemo, useState } from "react";
import type { AskDecide, DaemonClient } from "../../api/client";
import { PLAIN_MATCHES, UNITS, covered, limitsText, minutesText, scopeWithin, splitMinutes, toMinutes, type Unit } from "../../api/permissions";
import type { AllowAllPlanDTO, AskDecisionDTO, AskGroupDTO, AskResultDTO, InboxItemDTO, PermissionRuleDTO } from "../../api/schema";
import { plain } from "../../api/wording";
import { card, field } from "../../components/ui";
import { formatAge } from "../../store/overview";

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));
const requests = (n: number) => `${n} request${n === 1 ? "" : "s"}`;
const workers = (n: number) => `${n} worker${n === 1 ? "" : "s"}`;
const distinct = (list: { agent_id: string }[]) => new Set(list.map((a) => a.agent_id)).size;
const ago = (since: number) => {
  const age = formatAge(Date.now() / 1000 - since);
  return age === "just now" ? age : `${age} ago`;
};
const skippedText = (skipped: AskResultDTO[] | undefined) =>
  skipped?.length ? ` ${requests(skipped.length)} left waiting: ${skipped.map((s) => `${s.exact}${s.why ? ` (${s.why})` : ""}`).join(", ")}.` : "";

const ACT = "inline-flex h-6 items-center whitespace-nowrap rounded-md border px-2 text-[11.5px] font-medium disabled:opacity-50";
const PRIMARY = "border-sap bg-sap text-on-sap hover:opacity-90";
const PLAIN = "border-line-strong bg-surface text-ink hover:bg-surface-2";
const GHOST = "border-transparent text-ink-3 hover:text-ink";
const LINK = "text-[11.5px] text-branch hover:underline disabled:opacity-50";

/* The held requests, grouped, read again whenever the inbox changes. */
export function usePermissionAsks(client: DaemonClient, inbox: InboxItemDTO[]) {
  const [groups, setGroups] = useState<AskGroupDTO[]>([]);
  const reload = useCallback(() => client.permissionAsks().then(setGroups, () => undefined), [client]);
  useEffect(() => {
    void reload();
  }, [reload, inbox]);
  /* An /inbox card for a request one of these items holds. */
  const held = useCallback(
    (item: InboxItemDTO) => item.kind === "permission" && item.permission?.via === "hook" && groups.some((g) => g.items.some((a) => a.agent_id === item.agent_id && a.request_id === item.permission?.request_id)),
    [groups],
  );
  return { groups, reload, held };
}

export interface PermissionAsksProps {
  client: DaemonClient;
  groups: AskGroupDTO[];
  /* After a decision: the asks and the inbox read again. */
  onDone(): void;
  /* Settings › Permissions, with that rule's editor open (none: the unmatched setting). */
  onOpenRule?(rule?: string): void;
}

export function PermissionAsks({ client, groups, onDone, onOpenRule }: PermissionAsksProps) {
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<{ text: string; failed?: boolean } | null>(null);
  const run = async (work: () => Promise<string>) => {
    setBusy(true);
    try {
      setSaid({ text: await work() });
    } catch (e) {
      setSaid({ text: message(e), failed: true });
    } finally {
      setBusy(false);
      onDone();
    }
  };
  if (groups.length === 0 && !said) return null;
  return (
    <section className="flex flex-col gap-s1" aria-label="Permissions waiting">
      {groups.length > 0 && <StallNotice client={client} groups={groups} busy={busy} run={run} onOpenRule={onOpenRule} />}
      {said && (
        <p role={said.failed ? "alert" : "status"} className={`max-w-[960px] rounded-md px-s2 py-s1 text-[12.5px] ${said.failed ? "bg-sap-soft text-sap" : "bg-pass-soft text-pass"}`} data-testid="permission-resolution">
          {said.text}
        </p>
      )}
      {groups.map((g) => (
        <AskCard key={g.key} client={client} g={g} groups={groups} busy={busy} run={run} onOpenRule={onOpenRule} />
      ))}
    </section>
  );
}

type Run = (work: () => Promise<string>) => Promise<void>;

/* What a decision did, in a line. */
function decided(g: AskGroupDTO, got: AskDecisionDTO, duration: string): string {
  const resumed = got.resumed ?? [];
  const stopped = got.stopped ?? [];
  const tail = (list: AskResultDTO[], verb: string) => ` ${requests(list.length)} ${verb}${list.length ? ` (${workers(distinct(list))})` : ""}.`;
  switch (got.mode) {
    case "forever":
      return `Always allowed: ${got.scope.label}. Kept in Saved actions until you delete it.${tail(resumed, "resumed")}${skippedText(got.skipped)}`;
    case "session":
      return `Allowed for this session: ${got.scope.label}.${tail(resumed, "resumed")}${skippedText(got.skipped)}`;
    case "timed":
      return `Allowed for ${duration}: ${got.scope.label}.${tail(resumed, "resumed")}${skippedText(got.skipped)}`;
    case "once":
      return `Allowed once: ${resumed.map((r) => r.exact).join(", ")}. The next request like it asks again.${tail(resumed, "resumed")}`;
    case "always-deny":
      return `Denied and saved: ${got.scope.label}. Matching requests stop without asking; edit or delete the deny in Saved actions.${tail(stopped, "stopped")}`;
    default:
      return `Denied: ${g.title.replace(/^Allow /, "").replace(/\?$/, "")}. No decision saved.${tail(stopped, "stopped")}`;
  }
}

function AskCard({ client, g, groups, busy, run, onOpenRule }: { client: DaemonClient; g: AskGroupDTO; groups: AskGroupDTO[]; busy: boolean; run: Run; onOpenRule?(rule?: string): void }) {
  const fits = (value: string) => scopeWithin(g.scopes.find((s) => s.value === value)!, g.limits);
  const [scope, setScope] = useState(fits(g.general.value) ? g.general.value : (g.scopes.find((s) => scopeWithin(s, g.limits))?.value ?? g.general.value));
  const cap = g.limits?.life === "time" ? (g.limits.minutes ?? null) : null;
  const start = splitMinutes(cap !== null && cap < 30 ? cap : 30);
  const [amount, setAmount] = useState(String(start.value));
  const [unit, setUnit] = useState<Unit>(start.unit);
  const [help, setHelp] = useState<string | null>(null);
  const picked = g.scopes.find((s) => s.value === scope) ?? g.general;
  const modes = g.modes ?? [];
  const rep = g.items[0];
  const exact = g.scopes.find((s) => s.value === "exact");
  const minutes = toMinutes(Number(amount), unit);
  const duration = `${amount} ${Number(amount) === 1 ? unit.slice(0, -1) : unit}`;
  const total = groups.reduce((n, x) => n + x.items.length, 0);
  const hits = covered(groups, g, picked);
  const timed = modes.includes("timed");

  const decide = (mode: AskDecide["mode"]) => {
    if (mode === "timed" && !(minutes > 0)) return setHelp("Choose a valid positive duration.");
    if (mode === "timed" && cap !== null && minutes > cap) return setHelp(`Your rule lets an approval last up to ${minutesText(cap)}.`);
    setHelp(null);
    void run(async () => decided(g, await client.decidePermissionAsks({ key: g.key, scope: picked.value, mode, ...(mode === "timed" ? { minutes } : {}) }), duration));
  };

  return (
    <article id={`ask-${g.key}`} tabIndex={-1} className={`${card} relative flex max-w-[960px] scroll-mt-s3 flex-col gap-1 py-[11px] pl-[18px] pr-[14px] outline-none before:absolute before:bottom-[10px] before:left-0 before:top-[10px] before:w-[3px] before:rounded-sm before:bg-sap focus:ring-2 focus:ring-branch`} aria-label={g.title} data-testid="ask-card" data-key={g.key}>
      <div className="font-mono text-[10.5px] uppercase tracking-[0.06em] text-sap">
        Permission · {requests(g.items.length)} · {workers(distinct(g.items))}
        {g.blocked && " · blocked by a rule"}
      </div>
      <h3 className="text-[13.5px] font-semibold leading-[1.35]">{g.title}</h3>
      <p className="flex flex-wrap items-center gap-1 text-[12.5px] text-ink-2">
        Generalized from: <code className="font-mono text-xs">{rep.exact}</code>
        {exact && exact.value !== g.general.value && !g.blocked && (
          <button type="button" className={LINK} disabled={scope === "exact" || !fits("exact")} onClick={() => setScope("exact")}>
            Narrow to this request
          </button>
        )}
      </p>
      <ul className="ml-s2 list-disc text-[12.5px] text-ink-2" aria-label="Requests">
        {g.items.map((a) => (
          <li key={`${a.agent_id}:${a.request_id}`}>
            <code className="font-mono text-xs">{a.exact}</code>{" "}
            <small className="text-ink-3">
              · {a.worker || a.agent_id} · {ago(a.waiting_since)}
            </small>
          </li>
        ))}
      </ul>
      <div className="mt-1 border-l-2 border-branch px-s1 text-[12.5px]">
        <strong className="font-medium">{g.blocked ? "A deny rule blocks this." : g.rule ? `Your rule asks: ${limitsText(g.limits).toLowerCase()}.` : "No rule covers this, and unmatched actions ask you."}</strong>
        {onOpenRule && (g.rule || !g.blocked) && (
          <button type="button" className={`${LINK} ml-1`} onClick={() => onOpenRule(g.rule ?? undefined)}>
            {g.blocked ? "Change the rule that blocks this" : g.rule ? "Stop asking: change the rule" : "Add a rule"}
          </button>
        )}
      </div>
      {!g.blocked && (
        <>
          <div className="mt-1 flex flex-wrap items-center gap-s2 text-xs text-ink-3">
            <label className="flex items-center gap-1">
              Apply to
              <select className={`${field} max-w-full py-0.5 text-xs`} aria-label="Approval scope" value={scope} onChange={(e) => setScope(e.target.value)}>
                {g.scopes.map((s) => (
                  <option key={s.value} value={s.value} disabled={!scopeWithin(s, g.limits)}>
                    {s.label}
                  </option>
                ))}
              </select>
            </label>
            {timed && (
              <span className="flex items-center gap-1">
                Remember for
                <input type="number" min={1} className={`${field} w-16 py-0.5 text-xs`} aria-label="Approval duration" value={amount} onChange={(e) => setAmount(e.target.value)} />
                <select className={`${field} py-0.5 text-xs`} aria-label="Approval duration unit" value={unit} onChange={(e) => setUnit(e.target.value as Unit)}>
                  {UNITS.map((u) => (
                    <option key={u.value} value={u.value}>
                      {u.label}
                    </option>
                  ))}
                </select>
                {cap !== null && <span>Up to {minutesText(cap)}</span>}
              </span>
            )}
          </div>
          <p className="text-xs text-ink-3" data-testid="ask-resume">
            This grant resumes {hits.length === total ? `all ${requests(total)}` : `${requests(hits.length)} of ${total}`} ({workers(distinct(hits))}).
            {picked.value === "exact" && g.items.length > 1 ? ` Choose “${g.general.label}” to resume the other ${g.items.length - 1} in this item with one grant.` : ""}
          </p>
        </>
      )}
      <details className="text-xs text-ink-3">
        <summary className="cursor-pointer text-ink-2">Requests in full</summary>
        <pre className="mt-1 whitespace-pre-wrap rounded-md bg-canvas-2 p-s1 font-mono text-xs text-ink-2">{g.items.map((a) => a.summary).join("\n")}</pre>
      </details>
      {modes.length > 0 && (
        <div className="mt-1 flex flex-wrap items-center justify-end gap-1.5" role="group" aria-label="Decision">
          {modes.includes("deny") && (
            <button type="button" className={`${ACT} ${GHOST}`} disabled={busy} onClick={() => decide("deny")}>
              Deny
            </button>
          )}
          {modes.includes("always-deny") && (
            <button type="button" className={`${ACT} ${GHOST}`} disabled={busy} onClick={() => decide("always-deny")}>
              Always deny
            </button>
          )}
          {modes.includes("once") && (
            <button type="button" className={`${ACT} ${PLAIN}`} disabled={busy} onClick={() => decide("once")}>
              Allow once
            </button>
          )}
          {modes.includes("forever") && (
            <button type="button" className={`${ACT} ${PLAIN}`} disabled={busy} onClick={() => decide("forever")}>
              Always allow{picked.value === "exact" ? " this one" : ""}
            </button>
          )}
          {(timed || modes.includes("session")) && (
            <button type="button" className={`${ACT} ${PRIMARY}`} disabled={busy} onClick={() => decide(timed ? "timed" : "session")}>
              {timed ? `Allow for ${duration}` : "Allow for this session"}
            </button>
          )}
        </div>
      )}
      {help && (
        <p role="alert" className="text-xs text-sap">
          {help}
        </p>
      )}
    </article>
  );
}

/* The stall notice: what is blocking the workers, and the fixes for all of it at once. */
function StallNotice({ client, groups, busy, run, onOpenRule }: { client: DaemonClient; groups: AskGroupDTO[]; busy: boolean; run: Run; onOpenRule?(rule?: string): void }) {
  const items = groups.flatMap((g) => g.items);
  const blocked = groups.filter((g) => g.blocked);
  const denied = blocked.flatMap((g) => g.items);
  const allowed = items.length - denied.length;
  const longest = Math.min(...items.map((a) => a.waiting_since));
  const [plans, setPlans] = useState<{ now: AllowAllPlanDTO; forever: AllowAllPlanDTO } | null>(null);
  const [rules, setRules] = useState<PermissionRuleDTO[]>([]);
  const signature = useMemo(() => groups.map((g) => `${g.key}:${g.items.length}`).join("|"), [groups]);
  useEffect(() => {
    let live = true;
    void Promise.all([client.permissionAllowAllPlan(), client.permissionAllowAllPlan(undefined, true)]).then(
      ([now, forever]) => live && setPlans({ now, forever }),
      () => live && setPlans(null),
    );
    return () => {
      live = false;
    };
  }, [client, signature]);
  const needRules = blocked.some((g) => g.rule);
  useEffect(() => {
    if (needRules) void client.permissions().then((p) => setRules(p.rules), () => undefined);
  }, [client, needRules, signature]);
  const denyRules = [...new Set(blocked.map((g) => g.rule).filter((r): r is string => !!r))];
  // A pattern rule by its pattern; a built-in matcher by what it covers.
  const describe = (id: string) => {
    const r = rules.find((x) => x.id === id);
    return !r ? id : PLAIN_MATCHES.includes(r.match_type) ? r.match : r.describe;
  };
  const jump = (key: string) => {
    const el = document.getElementById(`ask-${key}`);
    el?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    el?.focus();
  };
  const allowAll = (forever: boolean) =>
    void run(async () => {
      const got = await client.permissionAllowAll(undefined, forever);
      const resumed = got.resumed ?? [];
      const scopes = [...new Set(got.grants.map((x) => x.scope.label))].join("; ");
      return `${forever ? "Always allowed" : "Allowed within each rule's limits"}: ${scopes}. ${requests(resumed.length)} resumed (${workers(distinct(resumed))}) with ${got.grants.length} grant${got.grants.length === 1 ? "" : "s"}.${got.skipped?.length ? ` ${requests(got.skipped.length)} still waiting: blocked or outside their limits.` : ""}`;
    });
  const unblock = (forever: boolean) =>
    void run(async () => {
      const got = await client.permissionUnblock(undefined, forever);
      const resumed = got.resumed ?? [];
      return `Unblocked: ${got.excepted ?? 0} exception${got.excepted === 1 ? "" : "s"} added to the deny${denyRules.length === 1 ? "" : "s"}${got.added ? `, ${got.added} rule${got.added === 1 ? "" : "s"} added` : ""}. ${requests(resumed.length)} resumed.${got.locked?.length ? ` ${requests(got.locked.length)} still blocked.` : ""}`;
    });
  const now = plans?.now.resumes ?? 0;
  const forever = plans?.forever.resumes ?? 0;

  return (
    <div className="flex max-w-[960px] flex-wrap items-center gap-s1 rounded-md border border-sap bg-sap-soft px-s2 py-s1 text-[13px]" role="status" data-testid="stall-notice">
      <span className="text-sap" aria-hidden>
        ●
      </span>
      <span>
        <strong className="font-medium">{workers(distinct(items))} paused waiting for you</strong> · {requests(items.length)} in {groups.length} item{groups.length === 1 ? "" : "s"} · longest {ago(longest)}
      </span>
      <span className="flex basis-full flex-wrap items-center gap-1 text-xs text-ink-3">
        Still blocking:
        {groups.map((g) => (
          <button key={g.key} type="button" className="rounded-md border border-line px-s1 text-xs text-branch" onClick={() => jump(g.key)}>
            {g.title.replace(/^Allow /, "").replace(/\?$/, "")} · {g.items.length} · {ago(g.items[0].waiting_since)}
            {g.blocked ? " · blocked by rule" : ""}
          </button>
        ))}
      </span>
      {allowed > 0 && now > 0 && (
        <span className="flex basis-full flex-wrap items-center gap-1">
          <button type="button" className={`${ACT} ${PRIMARY}`} disabled={busy} onClick={() => allowAll(false)}>
            {now === items.length ? `Allow all ${requests(now)}` : `Allow the ${requests(now)} your rules permit`}
          </button>
          {forever > 0 ? (
            <button type="button" className={`${ACT} ${PLAIN}`} disabled={busy} onClick={() => allowAll(true)}>
              Always allow {forever === items.length ? "all" : `these ${forever}`}
            </button>
          ) : (
            <span className="text-xs text-ink-3">Always allow unavailable: your rules limit how long these approvals may last.</span>
          )}
        </span>
      )}
      {denied.length > 0 && (
        <span className="flex basis-full flex-wrap items-center gap-1" data-testid="stall-deny">
          <button type="button" className={`${ACT} ${PRIMARY}`} disabled={busy} onClick={() => unblock(false)}>
            Allow these {denied.length}: {denyRules.length ? `add an exception to ${denyRules.length === 1 ? `the deny on ${describe(denyRules[0])}` : `${denyRules.length} deny rules`}` : "add rules for them"}
          </button>
          <button type="button" className={`${ACT} ${PLAIN}`} disabled={busy} onClick={() => unblock(true)}>
            Always allow these
          </button>
          {onOpenRule && (
            <button type="button" className={LINK} onClick={() => onOpenRule(denyRules[0])}>
              {denyRules.length ? "Open the deny rule" : "Change unmatched actions"}
            </button>
          )}
        </span>
      )}
    </div>
  );
}
