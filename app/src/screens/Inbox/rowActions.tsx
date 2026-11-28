/* What an open Inbox row holds for the kinds that are acted on with more than
   one answer (InboxQueue.tsx opens one row at a time): the item's detail and
   its own controls, as the engine offers them. One click, sent at once
   (POST /inbox/{id}/{action}), and the row goes; the engine's refusal, if
   any, comes back as one line on it (InboxQueue's note).

   - a plan-first offer: the session, the goal, its milestones, Yes / Not now;
   - a provider switch, a handover: their own choices;
   - proposed checks: the commands whole, Accept / Decline;
   - a setup problem the engine can repair: Fix, a job on the engine;
   - a lapsed sign-in: the provider's own sign-in, or brindle's cloud one;
   - a prepared PR: Open PR (the review sheet's call, without its diffs);
   - an opened or failed PR: Open on GitHub, or Review & open PR. */

import { useEffect, useRef, useState, type ReactNode } from "react";
import type { InboxAction } from "../../api/client";
import { useDaemon } from "../../api/context";
import type { OpenUrl } from "../../api/open";
import type { InboxItemDTO } from "../../api/schema";
import { PROVIDER_SWITCH_KINDS, ProviderSwitchCard, answerProviderSwitch, answerSwitchWorkers, isNotice, offerOf } from "../../components/ProviderSwitch/ProviderSwitch";
import type { Route } from "../../routes";
import { useJob } from "../Branch/useJob";
import { ItemActions } from "./ItemActions";
import { FIXES, describeItem, fixable, itemKey } from "./items";
import type { InboxActions } from "./useInboxActions";

const ACT = "btn btn-secondary text-xs";
const MAIN = "btn btn-primary text-xs";
const QUIET = "btn btn-quiet text-xs";

/* The item's detail, as the open row shows it under its headline. */
export function itemDetail(item: InboxItemDTO): ReactNode {
  if (item.kind === "plan_offer" && item.plan_offer) {
    const offer = item.plan_offer;
    const verified = offer.milestones.filter((m) => m.verified).length;
    return <div className="space-y-2 break-words" data-testid="card-detail">
      <p><strong>Session: {offer.session_name}</strong></p>
      <p className="whitespace-pre-wrap">{offer.goal}{offer.detail && `\n${offer.detail}`}</p>
      <div>
        <p data-testid="offer-progress">{verified} of {offer.milestones.length} milestones verified</p>
        <ul className="list-disc pl-4">
          {offer.milestones.map((m, index) => <li key={index}>{m.title}{m.verified ? " — verified" : ""}</li>)}
        </ul>
      </div>
      <p data-testid="offer-scale">{offer.workers} {offer.workers === 1 ? "worker" : "workers"} · {offer.repos} {offer.repos === 1 ? "repository" : "repositories"}</p>
      <p><strong>Why brindle is offering: </strong>{offer.reasons.join(" ") || "The work qualified when this offer was made; the plan has since changed."}</p>
      <p>Yes: the supervisor writes a plan for you to approve before more workers start. Not now: the work goes ahead as is.</p>
    </div>;
  }
  if (item.kind === "proposed_checks") {
    // Approved or accepted from this row: shown whole, never cut short.
    return <pre className="whitespace-pre-wrap font-mono text-xs" data-testid="card-detail">{item.detail}</pre>;
  }
  if (item.kind === "org_notice") return item.detail ? <span>from your org's {item.detail}</span> : null;
  if (item.kind === "doc") return <span>{item.detail ?? `Document ${item.doc}`}{item.section && ` · ${item.section}`}</span>;
  if (item.kind === "setup") {
    // What the button does, in a line; a fix this app doesn't know: the engine's detail, if any.
    const fix = item.fix ? FIXES[item.fix] : undefined;
    return fix ? <span>{fix.help}</span> : item.detail ? <span className="line-clamp-3">{item.detail}</span> : null;
  }
  if (item.kind === "question" || item.kind === "pr_ready" || item.kind === "review_requested") return null;
  if (item.detail && item.kind !== "credentials" && item.kind !== "login" && !PROVIDER_SWITCH_KINDS.includes(item.kind)) {
    return <span className="line-clamp-3">{item.detail}</span>;
  }
  if (item.kind === "signed_off") return null;
  if (item.reason && item.reason !== describeItem(item)) return <span>{item.reason}</span>;
  return null;
}

/* The controls a kind is answered with in its open row, or null when its row's one action is all it has. */
export function itemControls({ item, actions, repo, onDone, onGo, onReview, onNavigate, openUrl }: {
  item: InboxItemDTO;
  actions: InboxActions;
  repo: string | null;
  onDone(): void;
  onGo?(): void;
  onReview?(): void;
  onNavigate?(route: Route): void;
  openUrl?: OpenUrl;
}): ReactNode {
  const busy = actions.busy.has(itemKey(item));
  const act = (label: string, action: InboxAction, body: Record<string, unknown> = {}, look = ACT) => (
    <button key={label} type="button" className={look} disabled={busy} onClick={() => actions.run(item, action, body)}>
      {label}
    </button>
  );
  if (item.kind === "proposed_checks") return <>{act("Accept", "accept", { allow_commands: false }, MAIN)}{act("Decline", "decline", {}, QUIET)}</>;
  if (item.kind === "plan_offer") return <PlanOfferActions item={item} actions={actions} onDone={onDone} onGo={onGo} />;
  if (PROVIDER_SWITCH_KINDS.includes(item.kind)) return <ProviderSwitchActions item={item} actions={actions} onDone={onDone} />;
  if (item.kind === "handover" && item.choices?.length) return <HandoverActions item={item} actions={actions} onDone={onDone} />;
  if (fixable(item)) return <SetupFix item={item} actions={actions} onDone={onDone} />;
  if (item.kind === "credentials" || item.kind === "login") return <ItemActions item={item} actions={actions} repo={repo} onDone={onDone} onNavigate={onNavigate} compact hideDismiss />;
  if (item.kind === "pr_ready") {
    // Review stays available before opening the PR; only Open PR pushes.
    return <>
      <OpenPr item={item} actions={actions} onDone={onDone} openUrl={openUrl} />
      {onReview && <button type="button" className={ACT} onClick={onReview}>Review changes first</button>}
    </>;
  }
  if (item.kind === "pr_opened" || item.kind === "pr_failed") {
    const url = item.url;
    if (url && openUrl) return <button type="button" className={MAIN} onClick={() => { void openUrl(url); actions.run(item, "pr-not-now", {}); }}>Open on GitHub</button>;
    if (item.kind === "pr_failed" && onReview) return <button type="button" className={MAIN} onClick={onReview}>Review &amp; open PR</button>;
    return null;
  }
  return null;
}

/* Switch / Not now on a provider-switch offer, through the item's own route; the row leaves once the engine took the answer. */
function ProviderSwitchActions({ item, actions, onDone }: { item: InboxItemDTO; actions: InboxActions; onDone(): void }) {
  const client = useDaemon();
  return (
    <ProviderSwitchCard
      showLine={false}
      variant={isNotice(item) ? "notice" : "offer"}
      offer={offerOf(item)}
      onAnswer={(accept, always) => answerProviderSwitch(client, item, accept, undefined, always).then(
        () => { actions.settle(item); onDone(); },
        (e: unknown) => actions.report(item, e instanceof Error ? e.message : String(e)),
      )}
      /* Workers paused on the same limit: their answer goes to each; the row comes back without them. */
      workerChoices={item.choices ?? undefined}
      onWorkers={(choice) => answerSwitchWorkers(client, item, choice).then(
        () => { actions.settle(item); onDone(); },
        (e: unknown) => actions.report(item, e instanceof Error ? e.message : String(e)),
      )}
    />
  );
}

/* A worker whose provider ran out (engine brindle.handover): the item's own
   choices (continue on another model / wait / stop), the first as the main
   button. One answer at a time; the row goes once the engine has it. */
function HandoverActions({ item, actions, onDone }: { item: InboxItemDTO; actions: InboxActions; onDone(): void }) {
  const client = useDaemon();
  const [saving, setSaving] = useState(false);
  const pending = useRef(false);
  const answer = (choice: string) => {
    if (pending.current) return;
    pending.current = true;
    setSaving(true);
    void client
      .answerHandover(item.request_id ?? "", choice)
      .then(() => actions.settle(item), (e: unknown) => actions.report(item, e instanceof Error ? e.message : String(e)))
      .finally(() => {
        pending.current = false;
        setSaving(false);
        onDone();
      });
  };
  return (
    <>
      {(item.choices ?? []).map((c, i) => (
        <button key={c.id} type="button" className={i === 0 ? MAIN : QUIET} disabled={saving} onClick={() => answer(c.id)}>
          {c.label}
        </button>
      ))}
    </>
  );
}

/* brindle's one-time offer to plan a big goal first: Yes / Not now, answered through the goal
   route with the offer id the item carries (request_id). A refusal (the offer was replaced
   meanwhile) is the one line on the row; either way the inbox is read again. */
function PlanOfferActions({ item, actions, onDone, onGo }: { item: InboxItemDTO; actions: InboxActions; onDone(): void; onGo?(): void }) {
  const client = useDaemon();
  const [saving, setSaving] = useState(false);
  const pending = useRef(false);
  const answer = (accept: boolean) => {
    // Guard synchronously: both choices can fire before React disables them.
    if (pending.current) return;
    pending.current = true;
    setSaving(true);
    actions.dismiss(itemKey(item));
    void client
      .resolvePlanOffer(item.root_id ?? "", item.request_id ?? "", accept)
      .then(() => {
        actions.settle(item);
        onGo?.();
      }, (e: unknown) => actions.report(item, e instanceof Error ? e.message : String(e), true))
      .finally(() => {
        pending.current = false;
        setSaving(false);
        onDone();
      });
  };
  return (
    <>
      <button type="button" className={MAIN} disabled={saving} onClick={() => answer(true)}>
        Yes
      </button>
      <button type="button" className={QUIET} disabled={saving} onClick={() => answer(false)}>
        Not now
      </button>
    </>
  );
}

/* A setup item's one button (items.ts FIXES): one click starts the fix on
   the engine, a job followed on the bus. Done, the row goes (and the inbox
   is read again: the engine no longer lists the problem); failed, the job's
   error is the one line on the row, and the button is there to press again. */
function SetupFix({ item, actions, onDone }: { item: InboxItemDTO; actions: InboxActions; onDone(): void }) {
  const client = useDaemon();
  const job = useJob();
  const fix = FIXES[item.fix ?? ""];
  const status = job.job?.status;
  const error = job.job?.error;
  useEffect(() => {
    if (status === "failed") actions.report(item, error ?? "the fix failed", true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, error]);
  const start = () =>
    void job.start(fix.label, () => client[fix.route](), () => {
      actions.settle(item);
      onDone();
    });
  return (
    <button type="button" className={MAIN} disabled={job.busy} onClick={start} data-testid="setup-fix">
      {fix.label}
    </button>
  );
}

/* Open PR on a prepared PR: the review sheet's own call (POST /sessions/{root}/pr, a job) with the
   draft's title and description, without its diffs. The button waits while the job runs; a refusal shows on the row. */
export function OpenPr({ item, actions, onDone, openUrl, look = MAIN, onOpened }: { item: InboxItemDTO; actions: InboxActions; onDone(): void; openUrl?: OpenUrl; look?: string; onOpened?(): void }) {
  const client = useDaemon();
  const job = useJob();
  const [drafting, setDrafting] = useState(false);
  /* gh couldn't open it: the compare page, where the person finishes by hand (no PR exists yet). */
  const [compare, setCompare] = useState<string | null>(null);
  const status = job.job?.status;
  const error = job.job?.error;
  useEffect(() => {
    if (status === "failed") actions.report(item, error ?? "couldn't open the PR", true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, error]);
  const root = item.root_id ?? item.agent_id ?? "";
  const open = async () => {
    setDrafting(true);
    try {
      const draft = await client.prDraft(root);
      await job.start("Opening the PR", () => client.openSessionPr(root, { title: item.detail?.trim() || draft.title, body: draft.body }), (result) => {
        const url = (result as { url?: string } | undefined)?.url;
        if (url && !/\/pull\/\d+/.test(url)) {
          // No PR was created: nothing settles; the compare page is the way on.
          setCompare(url);
          actions.report(item, "GitHub's CLI couldn't open the PR; open it from the compare page.", true);
          return;
        }
        actions.settle(item);
        onOpened?.();
        onDone();
      });
    } catch (e) {
      actions.report(item, e instanceof Error ? e.message : String(e), true);
    } finally {
      setDrafting(false);
    }
  };
  const busy = drafting || job.busy;
  return (
    <>
      <button type="button" className={look} disabled={busy} onClick={(e) => { e.stopPropagation(); void open(); }} data-testid="open-pr">
        {busy ? "Opening…" : "Open PR"}
      </button>
      {compare && openUrl && (
        <button type="button" className={ACT} onClick={(e) => { e.stopPropagation(); void openUrl(compare); }}>
          Open on GitHub
        </button>
      )}
    </>
  );
}
