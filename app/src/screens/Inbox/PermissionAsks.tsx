/* The Inbox's permission cards (design/inbox-permission-card.html): one
   card for every request that needs a person, whoever holds it. A request
   brindle's hook holds comes grouped by the daemon (GET /permissions/asks),
   one card per kind and general form, decided at once for every request in
   it (POST /permissions/asks/decide); a prompt only the agent's own window
   can take is the same card with a note and a button to that window instead
   of the choices. The card says who asks for what, in a `group › item` chip
   (the capability group and subcategory the daemon's baseline named), why it
   reached the person, and three actions: Deny, Allow once and Allow ▾
   (this session, a duration, or always for the chip's group item, which
   sets that subcategory's override: PUT /permissions/groups; an exact
   request can still be saved as an action). The requests, the scope and the
   rule sit under Details, collapsed. Above the cards, the stall banner: one
   line, and a button to the first card. */

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import type { AskDecide, DaemonClient } from "../../api/client";
import { UNITS, covered, limitsText, minutesText, scopeWithin, splitMinutes, toMinutes, type Unit } from "../../api/permissions";
import type { AskDecisionDTO, AskGroupDTO, AskResultDTO, InboxItemDTO, OrgControlViewDTO } from "../../api/schema";
import { plain } from "../../api/wording";
import { card, field } from "../../components/ui";
import { formatAge } from "../../store/overview";
import { describeItem, itemKey, permissionAnswer, permissionLine, seenAt } from "./items";
import type { useInboxActions } from "./useInboxActions";

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

const BTN = "inline-flex min-h-8 items-center justify-center gap-2 whitespace-nowrap rounded-lg border px-4 text-sm font-medium disabled:opacity-50";
const DENY = "border-transparent text-ink-2 hover:bg-fail-soft hover:text-fail";
const PLAIN = "border-line-strong bg-surface text-ink hover:bg-surface-2";
const PRIMARY = "border-fawn bg-fawn text-on-fawn hover:opacity-90";
const LINK = "text-xs text-branch hover:underline disabled:opacity-50";

const GROUP_NAMES: Record<string, string> = { files: "Files", shell: "Shell", git: "Git", network: "Network", mcp: "MCP" };
const ITEM_NAMES: Record<string, string> = {
  read: "Read files",
  "write-worktree": "Write in the worktree",
  "write-outside": "Write outside the worktree",
  readonly: "Read-only commands",
  "dev-tools": "Dev tools",
  install: "Package installs",
  destructive: "Destructive commands",
  other: "Other commands",
  commit: "Commits",
  "push-feature": "Push to a feature branch",
  "push-protected": "Push to the default branch",
  fetch: "Fetch pages",
  search: "Web search",
  tool: "Tool calls",
};

/* The capability group and subcategory whose baseline made a request ask: the daemon's reason names them
   ("the shell/install baseline is ask"); none when a rule, or no group, asked. */
export function groupItem(reason: string | undefined): { group: string; sub: string; label: string } | null {
  const m = reason?.match(/the ([a-z]+)\/([a-z-]+) baseline is ask/);
  if (!m || !GROUP_NAMES[m[1]]) return null;
  const [, group, sub] = m;
  return { group, sub, label: `${GROUP_NAMES[group]} › ${ITEM_NAMES[sub] ?? sub}` };
}

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

type Run = (work: () => Promise<string>) => Promise<void>;

export function PermissionAsks({ client, groups, onDone, onOpenRule }: PermissionAsksProps) {
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<{ text: string; failed?: boolean } | null>(null);
  const inflight = useRef(false);
  const run: Run = async (work) => {
    // One decision at a time: a second click while one is out waits for its answer.
    if (inflight.current) return;
    inflight.current = true;
    setBusy(true);
    try {
      setSaid({ text: await work() });
    } catch (e) {
      setSaid({ text: message(e), failed: true });
    } finally {
      inflight.current = false;
      setBusy(false);
      onDone();
    }
  };
  if (groups.length === 0 && !said) return null;
  return (
    <section className="flex flex-col gap-s1" aria-label="Permissions waiting">
      {groups.length > 0 && <StallBanner groups={groups} />}
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

/* The one permission card: who asks for what, its chip, why it reached the person, the actions it is
   given, and the details collapsed. `window` (an agent's own prompt) is branch-coloured, a held request sap. */
export function PermissionCard({ label, source, id, dataKey, who, what, chip, age, why, note, details, children }: {
  label: string;
  source: "hook" | "window";
  id?: string;
  dataKey?: string;
  who: string;
  what: ReactNode;
  chip: string | null;
  age: string;
  why: ReactNode;
  note?: ReactNode;
  details: ReactNode;
  children?: ReactNode;
}) {
  const window = source === "window";
  return (
    <article id={id} tabIndex={-1} aria-label={label} data-testid="permission-card" data-source={source} data-key={dataKey} className={`${card} relative flex max-w-[960px] scroll-mt-s3 flex-col gap-s1 py-s2 pl-5 pr-4 outline-none before:absolute before:bottom-3 before:left-0 before:top-3 before:w-[3px] before:rounded-sm focus:ring-2 focus:ring-branch ${window ? "before:bg-branch" : "before:bg-sap"}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-[15px] font-semibold leading-[1.4]">
            {who} <span className="font-normal text-ink-2">{what}</span>
          </h3>
          {chip && (
            <span className={`mt-2 inline-flex items-center rounded border px-1.5 py-0.5 text-xs ${window ? "border-branch bg-branch-soft text-branch" : "border-line text-ink-2"}`} data-testid="permission-chip">
              {chip}
            </span>
          )}
        </div>
        <span className="whitespace-nowrap pt-0.5 text-xs text-ink-3">{age}</span>
      </div>
      <p className="flex items-center gap-2 text-[13px] text-ink-2" data-testid="permission-why">
        <span className={`size-1.5 flex-none rounded-full ${window ? "bg-branch" : "bg-sap"}`} aria-hidden />
        <span>{why}</span>
      </p>
      {note}
      {children}
      <details className="border-t border-line pt-2 text-xs text-ink-3">
        <summary className="cursor-pointer text-ink-3 hover:text-ink">Details</summary>
        <div className="mt-2 flex flex-col gap-2">{details}</div>
      </details>
    </article>
  );
}

/* Allow ▾: the Allow button, and the other ways to allow in a menu. */
function AllowSplit({ label, disabled, onPrimary, children }: { label: string; disabled: boolean; /* none: Allow only opens the menu */ onPrimary?(): void; children(): ReactNode }) {
  const [open, setOpen] = useState(false);
  const close = () => setOpen(false);
  return (
    <div className="relative inline-flex" onKeyDown={(e) => e.key === "Escape" && close()}>
      <button type="button" className={`${BTN} ${PRIMARY} rounded-r-none`} disabled={disabled} onClick={onPrimary ?? (() => setOpen((o) => !o))}>
        {label}
      </button>
      <button type="button" className={`${BTN} ${PRIMARY} min-w-8 rounded-l-none border-l border-l-on-fawn/35 px-2.5`} disabled={disabled} aria-haspopup="menu" aria-expanded={open} aria-label="More ways to allow" onClick={() => setOpen((o) => !o)}>
        ▾
      </button>
      {open && (
        // An item closes the menu once it has run, unless it only opens more of it (`keep`).
        <div role="menu" aria-label="Allow" className="absolute right-0 top-[calc(100%+6px)] z-20 flex min-w-72 flex-col rounded-lg border border-line-strong bg-surface p-1 shadow-lg" onClick={(e) => (e.target as HTMLElement).closest("[role=menuitem]:not([data-keep]), [data-close]") && close()}>
          {children()}
        </div>
      )}
    </div>
  );
}

function MenuItem({ title, hint, keep, disabled, onClick }: { title: string; hint: string; keep?: boolean; disabled?: boolean; onClick(): void }) {
  return (
    <button type="button" role="menuitem" disabled={disabled} data-keep={keep ? "" : undefined} className="flex w-full flex-col items-start rounded-md px-2.5 py-2 text-left text-sm font-medium hover:bg-surface-2 disabled:opacity-50" onClick={onClick}>
      {title}
      <small className="text-xs font-normal text-ink-3">{hint}</small>
    </button>
  );
}

/* Why an org control holds this: its name and plain reason, never a rule id. Only the org changes it. */
function OrgWhy({ org, blocked }: { org: OrgControlViewDTO; blocked?: boolean }) {
  return (
    <span data-testid="org-why" data-org={org.name} data-reason={org.reason} data-setting={blocked ? "deny" : "ask"}>
      <span className="font-medium text-ink-2">{blocked ? `Blocked by ${org.name} policy` : "Your organization asks for approval"}</span>
      {` · ${blocked ? "" : `${org.name}: `}${org.reason || `set for ${org.what}`}.`}
      <span className="text-ink-3"> Only an organization admin can change this.</span>
    </span>
  );
}

/* A request brindle's hook holds, with the others like it. */
function AskCard({ client, g, groups, busy, run, onOpenRule }: { client: DaemonClient; g: AskGroupDTO; groups: AskGroupDTO[]; busy: boolean; run: Run; onOpenRule?(rule?: string): void }) {
  const fits = (value: string) => scopeWithin(g.scopes.find((s) => s.value === value)!, g.limits);
  const [scope, setScope] = useState(fits(g.general.value) ? g.general.value : (g.scopes.find((s) => scopeWithin(s, g.limits))?.value ?? g.general.value));
  const cap = g.limits?.life === "time" ? (g.limits.minutes ?? null) : null;
  const start = splitMinutes(cap !== null && cap < 30 ? cap : 30);
  const [amount, setAmount] = useState(String(start.value));
  const [unit, setUnit] = useState<Unit>(start.unit);
  const [timing, setTiming] = useState(false);
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
  const session = modes.includes("session");
  const slot = g.rule || g.blocked ? null : groupItem(rep.reason);
  const subject = g.title.replace(/^Allow /, "").replace(/\?$/, "");
  const names = [...new Set(g.items.map((a) => a.worker || a.agent_id))];

  const decide = (mode: AskDecide["mode"], inMinutes = minutes, forScope = picked.value) => {
    if (mode === "timed" && !(inMinutes > 0)) return setHelp("Choose a valid positive duration.");
    if (mode === "timed" && cap !== null && inMinutes > cap) return setHelp(`${g.org ? `${g.org.name} lets` : "Your rule lets"} an approval last up to ${minutesText(cap)}.`);
    setHelp(null);
    void run(async () => decided(g, await client.decidePermissionAsks({ key: g.key, scope: forScope, mode, ...(mode === "timed" ? { minutes: inMinutes } : {}) }), duration));
  };
  /* Always for the chip's group item: that subcategory's override becomes allow (the baseline's answer, not a
     saved action), and the requests waiting on it resume once. */
  const alwaysForItem = () => {
    if (!slot) return;
    const via: AskDecide["mode"] = modes.includes("once") ? "once" : session ? "session" : "timed";
    void run(async () => {
      await client.setPermissionGroups({ [slot.group]: { subcategories: { [slot.sub]: "allow" } } });
      const got = await client.decidePermissionAsks({ key: g.key, scope: picked.value, mode: via, ...(via === "timed" ? { minutes: cap ?? 30 } : {}) });
      const resumed = got.resumed ?? [];
      return `Always allowed: ${slot.label}. Saved as an override in Settings › Permissions. ${requests(resumed.length)} resumed (${workers(distinct(resumed))}).`;
    });
  };
  const allowable = !g.blocked && (session || timed);

  return (
    <PermissionCard
      label={g.title}
      source="hook"
      id={`ask-${g.key}`}
      dataKey={g.key}
      who={names.join(", ")}
      what={`asks to allow ${subject}`}
      chip={slot?.label ?? null}
      age={`waiting ${ago(Math.min(...g.items.map((a) => a.waiting_since))).replace(/ ago$/, "")}`}
      why={
        g.org ? (
          <OrgWhy org={g.org} blocked={g.blocked} />
        ) : (
        <>
          {rep.escalation && !g.blocked ? rep.escalation : g.blocked ? "A deny rule blocks this." : g.rule ?`Your rule asks: ${limitsText(g.limits).toLowerCase()}.` : slot ? `${slot.label} is set to ask.` : (rep.reason ? plain(rep.reason) : "No rule covers this, and unmatched actions ask you.")}
          {onOpenRule && (g.rule || !g.blocked) && (
            <button type="button" className={`${LINK} ml-2`} onClick={() => onOpenRule(g.rule ?? undefined)}>
              {g.blocked ? "Change the rule that blocks this" : g.rule ? "Change the rule" : "Add a rule"}
            </button>
          )}
        </>
        )
      }
      details={
        <>
          <ul className="flex flex-col gap-1 text-xs text-ink-2" aria-label="Requests">
            {g.items.map((a) => (
              <li key={`${a.agent_id}:${a.request_id}`}>
                <code className="font-mono text-xs">{a.exact}</code>{" "}
                <small className="text-ink-3">
                  · {a.worker || a.agent_id} · {ago(a.waiting_since)}
                </small>
              </li>
            ))}
          </ul>
          <pre className="whitespace-pre-wrap rounded-md border border-line bg-canvas-2 p-s1 font-mono text-xs text-ink-2">{g.items.map((a) => a.summary).join("\n")}</pre>
          {!g.blocked && (
            <>
              <div className="flex flex-wrap items-center gap-2">
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
                {exact && exact.value !== g.general.value && (
                  <button type="button" className={LINK} disabled={scope === "exact" || !fits("exact")} onClick={() => setScope("exact")}>
                    Narrow to this request
                  </button>
                )}
              </div>
              <p data-testid="ask-resume">
                This grant resumes {hits.length === total ? `all ${requests(total)}` : `${requests(hits.length)} of ${total}`} ({workers(distinct(hits))}).
                {picked.value === "exact" && g.items.length > 1 ? ` Choose “${g.general.label}” to resume the other ${g.items.length - 1} in this item with one grant.` : ""}
              </p>
            </>
          )}
        </>
      }
    >
      {modes.length > 0 && (
        <div className="mt-1 flex flex-wrap items-center justify-end gap-2" role="group" aria-label="Decision">
          {modes.includes("deny") && (
            <button type="button" className={`${BTN} ${DENY}`} disabled={busy} onClick={() => decide("deny")}>
              Deny
            </button>
          )}
          {modes.includes("once") && (
            <button type="button" className={`${BTN} ${PLAIN}`} disabled={busy} onClick={() => decide("once")}>
              Allow once
            </button>
          )}
          {allowable && (
            <AllowSplit label="Allow" disabled={busy} onPrimary={() => decide(session ? "session" : "timed")}>
              {() => (
                <>
                  {session && <MenuItem disabled={busy} title="For this session" hint="Until the session ends" onClick={() => decide("session")} />}
                  {timed && <MenuItem keep disabled={busy} title={`For ${duration}…`} hint="Choose how long" onClick={() => setTiming(true)} />}
                  {timing && timed && (
                    <div className="flex items-center gap-2 px-2.5 py-2 text-[13px] text-ink-2">
                      <input type="number" min={1} className={`${field} w-16 py-0.5 text-xs`} aria-label="Approval duration" value={amount} onChange={(e) => setAmount(e.target.value)} />
                      <select className={`${field} py-0.5 text-xs`} aria-label="Approval duration unit" value={unit} onChange={(e) => setUnit(e.target.value as Unit)}>
                        {UNITS.map((u) => (
                          <option key={u.value} value={u.value}>
                            {u.label}
                          </option>
                        ))}
                      </select>
                      {cap !== null && <span>Up to {minutesText(cap)}</span>}
                      <button type="button" data-close className={`${BTN} ${PLAIN} ml-auto min-h-7 px-3`} disabled={busy} onClick={() => decide("timed")}>
                        Allow for {duration}
                      </button>
                    </div>
                  )}
                  {slot && <MenuItem disabled={busy} title={`Always for ${slot.label}`} hint="Saved as an override in Settings" onClick={alwaysForItem} />}
                  {modes.includes("forever") && exact && fits("exact") && <MenuItem disabled={busy} title="Always allow this exact request" hint="Kept in Saved actions" onClick={() => decide("forever", minutes, "exact")} />}
                </>
              )}
            </AllowSplit>
          )}
        </div>
      )}
      {help && (
        <p role="alert" className="text-xs text-sap">
          {help}
        </p>
      )}
    </PermissionCard>
  );
}

/* A permission /inbox lists as an item of its own: a prompt only the agent's window can take (a note, and the
   way to that window), or a hold brindle's hook has no group for (the provider's own choices). */
export function PermissionItemCard({ item, actions, who, onGo }: { item: InboxItemDTO; actions: ReturnType<typeof useInboxActions>; who: string; onGo?(): void }) {
  const p = item.permission;
  const line = permissionLine(item);
  const choices = p?.terminal ? [] : (p?.choices ?? []);
  const busy = actions.busy.has(itemKey(item));
  const answer = (choice: string) => actions.run(item, "permission", permissionAnswer(item, choice));
  const once = choices.find((c) => c.choice.endsWith("once"));
  const deny = choices.find((c) => c.choice === "deny");
  const more = choices.filter((c) => c !== once && c !== deny);
  // Allow itself answers only with an explicit session choice; a permanent one stays a labelled menu item.
  const session = more.find((c) => c.choice.includes("session"));
  const sent = actions.notes[itemKey(item)];
  const first = (line.lead ?? line.whole).split("\n")[0];
  const window = choices.length === 0;
  const age = formatAge((Date.now() - seenAt(item)) / 1000);
  return (
    <PermissionCard
      label={`${who} permission`}
      source={window ? "window" : "hook"}
      who={who}
      what={window ? <>asks: {first}{line.code && <> <code className="font-mono text-[13px]">{line.code}</code></>}</> : <>wants to run <code className="font-mono text-[13px]">{line.code ?? line.whole}</code></>}
      chip={window ? "Agent prompt" : null}
      age={`waiting ${age}`}
      why={window ? (p?.terminal ?? "Brindle can't answer this one: it is a question for the agent itself") : (item.reason || "Held by brindle")}
      note={
        <>
          {sent && (
            <p role={sent.failed ? "alert" : "status"} className={`text-xs ${sent.failed ? "text-sap" : "text-pass"}`}>
              {sent.text}
            </p>
          )}
          {window && (
            <div className="flex items-center gap-2 rounded-lg bg-branch-soft px-3 py-2 text-[13px] font-medium text-branch" data-testid="permission-terminal">
              <span className="flex-1">Answer in the agent’s window</span>
              {onGo && (
                <button type="button" className={`${BTN} min-h-7 border-line-strong px-3 text-branch`} onClick={onGo}>
                  Open {who}
                </button>
              )}
            </div>
          )}
        </>
      }
      details={
        <>
          <pre className="whitespace-pre-wrap rounded-md border border-line bg-canvas-2 p-s1 font-mono text-xs text-ink-2">{line.whole || describeItem(item)}</pre>
        </>
      }
    >
      {!window && (
        <div className="mt-1 flex flex-wrap items-center justify-end gap-2" role="group" aria-label="Decision">
          {deny && (
            <button type="button" className={`${BTN} ${DENY}`} disabled={busy} title={deny.rule ?? undefined} onClick={() => answer(deny.choice)}>
              Deny
            </button>
          )}
          {once && (
            <button type="button" className={`${BTN} ${PLAIN}`} disabled={busy} title={once.rule ?? undefined} onClick={() => answer(once.choice)}>
              Allow once
            </button>
          )}
          {more.length > 0 && (
            <AllowSplit label="Allow" disabled={busy} onPrimary={session ? () => answer(session.choice) : undefined}>
              {() => more.map((c) => <MenuItem key={c.choice} title={c.label} hint={c.rule ?? "Answered at once"} onClick={() => answer(c.choice)} disabled={busy} />)}
            </AllowSplit>
          )}
        </div>
      )}
    </PermissionCard>
  );
}

/* The stall banner: one line, and the way to the first card. The cards carry every action. */
function StallBanner({ groups }: { groups: AskGroupDTO[] }) {
  const items = groups.flatMap((g) => g.items);
  const review = () => {
    const el = document.getElementById(`ask-${groups[0].key}`);
    el?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    el?.focus();
  };
  return (
    <div className="flex max-w-[960px] items-center gap-2 rounded-lg border border-sap bg-sap-soft py-1.5 pl-3 pr-2 text-[13px]" role="status" data-testid="stall-notice">
      <span className="text-sap" aria-hidden>
        ●
      </span>
      <span>
        <strong className="font-medium">{workers(distinct(items))} waiting</strong> · {requests(items.length)} in {groups.length} item{groups.length === 1 ? "" : "s"}
      </span>
      <button type="button" className="ml-auto rounded-md px-2 py-0.5 text-branch hover:bg-surface-2" onClick={review}>
        Review all
      </button>
    </div>
  );
}
