/* Agents (A1, "separate layers", as the owner approved it on 2026-11-23):
   two choices, kept apart.

   - "Needs you": what stops a size's first pick (the org refuses it, its
     check found problems, its provider is signed out or isn't connected),
     each with the one action that fixes it.
   - "Who does the work": the agents. An agent is how to work (what it's
     for, instructions, rules), never which model: its model follows the job
     size, except where it keeps one of its own for a size. A custom profile
     that pins a model runs its own. "New agent" opens the add-an-agent form
     above the list (one column: Name, What it's for, Instructions, Rules,
     then "Works like a Developer, and its model follows the job size ·
     Change"); Edit opens the same form under a custom agent, or a built-in
     agent's own models per size.
   - "What model runs it": one shared table by job size (Light, Medium,
     Heavy, Advanced): what runs it, then the ordered fallbacks at its limit.
     Clicking the model picks another, the fallbacks edit the order, Edit
     holds the lock against hosted learning, the effort and instructions.
     When learning runs something else, the row runs it, tagged "learned",
     with "use mine instead"; when the first pick can't run, the one that
     does is shown "standing in".

   Nothing opens over the page: lists, panels and the form are inline. No
   <select>. Every change goes straight to the engine (PUT /v1/routing
   `kinds` and `agents`, POST and PUT /v1/profiles, into the person's
   ~/.brindle: agents are theirs, one list whatever the repos) and the page
   reads GET /v1/routing again: the rows at once, what learning runs after. */

import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { DaemonClient, KindPut, RulePackInfo, RoutingPut, RoutingScope, WorksLike } from "../../api/client";
import type { RoutingDTO } from "../../api/schema";
import { openExternal } from "../../api/open";
import { plain } from "../../api/wording";
import { PageHeader } from "../../components/PageHeader";
import { NO_REPO } from "../../components/RepoSelect";
import { alert, button, card, field, h2, label, main, mono, pill, quiet, softCheck, softField } from "../../components/ui";
import { useSessions } from "../../store/sessions";
import {
  COST_MARK,
  COST_WORDS,
  EFFORT,
  KIND_INFO,
  SIZES,
  WORKS_LIKE,
  agentLabel,
  agentRows,
  choiceOf,
  cleanName,
  connectedProviders,
  draftProblem,
  kindsOf,
  missingChoice,
  move,
  newDraft,
  offeredFor,
  problemCount,
  productName,
  providersOf,
  runOrder,
  type AgentRow,
  type Choice,
  type Draft,
  type Kind,
  type KindState,
  type Size,
} from "./agents";
import { Mark } from "./Mark";

export interface AgentsScreenProps {
  client: DaemonClient;
  /* Where a link on the page goes: Settings › Providers ("Sign in"), Knowledge's rules ("See all rules"). */
  onNavigate?(to: "providers" | "knowledge"): void;
}

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

/* An https link in a message is a real link that opens in the system browser (api/open.ts); the text is otherwise as given. */
function withLinks(text: string): ReactNode[] {
  return text.split(/(https:\/\/[^\s'"<>]*[^\s'"<>.,;:!?)])/g).map((part, i) =>
    i % 2 === 1 ? (
      <a
        key={i}
        href={part}
        className="underline"
        onClick={(e) => {
          e.preventDefault();
          void openExternal(part).catch(() => undefined);
        }}
      >
        {part}
      </a>
    ) : (
      part
    ),
  );
}

/* A link-like action in running text. */
const link = "text-branch underline underline-offset-2";

/* What is open under a size row: its list, its fallbacks, or its Edit. */
type Panel = "pick" | "fb" | "edit";

function CostTag({ cost }: { cost: string | null }) {
  if (!cost) return null;
  return (
    <span className={`${mono} text-xs text-ink-3`} title={COST_WORDS[cost]} aria-label={COST_WORDS[cost] ?? cost}>
      {COST_MARK[cost] ?? cost}
    </span>
  );
}

/* A model's effort, as a small boxed tag beside its name. */
function EffortChip({ effort }: { effort: string | null }) {
  if (!effort) return null;
  return (
    <span className="inline-flex items-center rounded-sm border border-line-strong px-[5px] text-[11px] font-medium leading-[15px] text-ink-2" data-effort={effort}>
      {effort}
    </span>
  );
}

/* A worker named by its model: the engine's display name ("Claude Opus"), or
   a custom profile's own name with its model quieter beside it. */
function ChoiceName({ c }: { c: Choice }) {
  return (
    <>
      <Mark provider={c.provider} />
      <span className={c.custom ? mono : ""} data-model-name={c.custom ? undefined : c.display}>
        {c.label}
      </span>
      {c.custom && c.model && <span className="text-ink-3">· {c.display}</span>}
      <EffortChip effort={c.effort} />
    </>
  );
}

/* The second line: "<maker> · runs in <tool> · <cost>". */
function MadeBy({ c, indent }: { c: Choice; indent?: boolean }) {
  if (!c.madeBy && !c.cost) return null;
  return (
    <span className={`flex items-baseline gap-1 whitespace-nowrap text-xs text-ink-3 ${indent ? "pl-[22px]" : ""}`} data-made-by>
      {c.madeBy}
      {c.madeBy && c.cost && <span aria-hidden>·</span>}
      <CostTag cost={c.cost} />
    </span>
  );
}

/* A one-of-several choice as a row of buttons (never a <select>). */
function Seg<T extends string>({ name, options, value, onPick, disabled, labelOf }: { name: string; options: readonly T[]; value: T | null; onPick(v: T): void; disabled?: boolean; labelOf?(v: T): string }) {
  return (
    <span role="group" aria-label={name} className="inline-flex flex-wrap gap-0.5">
      {options.map((o) => (
        <button
          key={o}
          type="button"
          aria-pressed={value === o}
          disabled={disabled}
          className={`rounded-md px-s1 py-0.5 text-sm ${value === o ? "bg-surface-2 font-medium text-ink" : "text-ink-2 hover:text-ink"}`}
          onClick={() => onPick(o)}
        >
          {labelOf ? labelOf(o) : o}
        </button>
      ))}
    </span>
  );
}

/* The link every "isn't connected" carries: Settings › Providers. */
function ConnectLink({ provider, onNavigate, children }: { provider: string; onNavigate?: AgentsScreenProps["onNavigate"]; children?: ReactNode }) {
  return (
    <button type="button" className={`text-sm ${link}`} data-connect={provider} onClick={() => onNavigate?.("providers")}>
      {children ?? `Connect ${productName(provider)}`}
    </button>
  );
}

/* The models for a slot, grouped: the person's custom profiles that pin a
   model, each connected provider's models (cost, speed, what it's good at),
   then one quiet line per provider that isn't connected. Inline, under the row. */
function Choices({ name, choices, current, skip = [], onPick, missing, onNavigate, extra }: {
  name: string;
  choices: Choice[];
  current?: string | null;
  skip?: string[];
  onPick(id: string): void;
  missing: string[];
  onNavigate?: AgentsScreenProps["onNavigate"];
  /* A first line before the models (an agent's "Follow the job size"). */
  extra?: ReactNode;
}) {
  /* What can run is offered; one of a connected provider's that can't (its
     own sign-in is out, its provider is excluded, a check found problems)
     is listed, dimmed, with why. A provider that isn't connected is one line. */
  const shown = choices.filter((c) => !skip.includes(c.id) && (c.ok || (c.blocker !== null && c.blocker !== "not_installed" && c.blocker !== "refused" && !missing.includes(c.provider))));
  const own = shown.filter((c) => c.custom);
  const byProvider = [...new Set(shown.filter((c) => !c.custom).map((c) => c.provider))];
  const option = (c: Choice) => (
    <li key={c.id}>
      {!c.ok ? (
        <span className="grid w-full grid-cols-[minmax(0,18rem)_minmax(0,1fr)] items-baseline gap-x-s2 px-s1 py-0.5 text-sm text-ink-3" data-choice-off={c.id}>
          <span className="flex min-w-0 flex-col">
            <span className="flex min-w-0 items-center gap-1.5">
              <ChoiceName c={c} />
            </span>
            <MadeBy c={c} />
          </span>
          <span className="truncate text-xs text-sap">{c.why}</span>
        </span>
      ) : (
        <button
          type="button"
          className={`grid w-full grid-cols-[minmax(0,18rem)_minmax(0,1fr)] items-baseline gap-x-s2 rounded-md px-s1 py-0.5 text-left text-sm hover:bg-surface-2 ${c.id === current ? "bg-pass-soft" : ""}`}
          aria-current={c.id === current ? "true" : undefined}
          data-choice={c.id}
          onClick={() => onPick(c.id)}
        >
          <span className="flex min-w-0 flex-col">
            <span className="flex min-w-0 items-center gap-1.5">
              <ChoiceName c={c} />
            </span>
            <MadeBy c={c} />
          </span>
          <span className="truncate text-xs text-ink-2">{c.good}</span>
        </button>
      )}
    </li>
  );
  const group = (title: ReactNode, list: Choice[], key: string) =>
    list.length > 0 && (
      <div key={key} className="flex flex-col gap-0.5">
        <div className={`${label} flex items-center gap-1 px-s1`}>{title}</div>
        <ul className="flex flex-col">{list.map(option)}</ul>
      </div>
    );
  return (
    <div className="flex flex-col gap-s1 py-s1" role="group" aria-label={`Choices for ${name}`} data-testid="choices">
      {extra}
      {group("Your own", own, "own")}
      {byProvider.map((p) => group(<><Mark provider={p} />{productName(p)}</>, shown.filter((c) => !c.custom && c.provider === p), p))}
      {missing.map((p) => (
        <p key={p} className="flex flex-wrap items-center gap-1 px-s1 text-xs text-ink-3" data-missing={p}>
          <Mark provider={p} />
          {productName(p)} isn't connected, so its models aren't offered.
          <ConnectLink provider={p} onNavigate={onNavigate}>
            Connect it in Settings › Providers
          </ConnectLink>
        </p>
      ))}
    </div>
  );
}

/* What an automatic check found, under the row it's used in. */
function Problems({ c }: { c: Choice }) {
  return (
    <div className="flex flex-col gap-0.5 rounded-md bg-sap-soft px-s2 py-s1 text-sm" data-problems={c.id}>
      {c.problems.map((p) => (
        <p key={p} className="flex gap-1 text-ink">
          <span className="text-sap" aria-hidden>
            ✕
          </span>
          {p}
        </p>
      ))}
      <p className="text-xs text-ink-2">
        Found when brindle checked <span className={mono}>{c.id}</span>. Until it's fixed in its file, routing skips it and uses the next fallback.
      </p>
    </div>
  );
}

const ProblemsButton = ({ c, open, onToggle }: { c: Choice; open: boolean; onToggle(): void }) =>
  c.problems.length > 0 ? (
    <button type="button" className={`${pill} text-sap`} aria-expanded={open} data-testid="problems" onClick={onToggle}>
      {problemCount(c.problems.length)} {open ? "▴" : "▾"}
    </button>
  ) : null;

/* Why a fallback is skipped, in two or three words beside it. */
function shortWhy(c: Choice): string {
  switch (c.blocker) {
    case "signed_out":
      return "signed out";
    case "not_installed":
      return "not connected";
    case "excluded":
      return "excluded";
    case "refused":
      return "not allowed";
    case "problems":
      return problemCount(c.problems.length);
    default:
      return c.why;
  }
}

/* Why a first pick can't run, after its name: "isn't allowed by your org". */
function cantRun(c: Choice): string {
  switch (c.blocker) {
    case "refused":
      return "isn't allowed by your org";
    case "problems":
      return `has ${problemCount(c.problems.length)}`;
    default:
      return `can't run: ${c.why}`;
  }
}

/* The first entry that can run, where the size's first pick can't. */
const runner = (list: Choice[]): Choice | undefined => list.find((c) => c.ok);

interface SizeRowProps {
  size: Size;
  state: KindState;
  choiceOf(id: string): Choice;
  choices: Choice[];
  missing: string[];
  panel: Panel | null;
  adding: boolean;
  /* The agent whose problems are open under the row, if any. */
  probs: string | null;
  busy: boolean;
  onPanel(p: Panel | null): void;
  onAdding(): void;
  onProbs(id: string): void;
  put(change: KindPut): void;
  onNavigate?: AgentsScreenProps["onNavigate"];
}

/* One job size in the shared table: what runs it, then at its limit. */
function SizeRow({ size, state, choiceOf: info, choices, missing, panel, adding, probs, busy, onPanel, onAdding, onProbs, put, onNavigate }: SizeRowProps) {
  const { name, blurb } = KIND_INFO[size];
  const list = runOrder(state).map(info);
  const [first, ...rest] = list;
  const now = runner(list);
  // What the row shows as running it: the first pick, or the one standing in for it.
  const shown = first && !first.ok && now ? now : first;
  const standingIn = !!first && shown !== first;
  const chain = list.map((c, i) => ({ c, at: i + 1 })).filter(({ c }) => c !== first && c !== shown);
  const learned = state.learned ? info(state.learned) : null;
  const own = [state.model, ...state.fallbacks].filter((x): x is string => !!x);
  const broken = list.find((c) => c.id === probs && c.problems.length > 0);
  const problems = (c: Choice) => <ProblemsButton c={c} open={probs === c.id} onToggle={() => onProbs(c.id)} />;
  const toggle = (p: Panel) => onPanel(panel === p ? null : p);
  const [instr, setInstr] = useState(state.instructions ?? "");
  useEffect(() => setInstr(state.instructions ?? ""), [state.instructions]);

  const fallbacks = (
    <div className="flex flex-col gap-s1 py-s1" data-testid="fallback-list">
      <p className="text-xs text-ink-2">Tried in order when the one before it is at its limit or can't run. None left: the task waits and you're told.</p>
      <ol className="flex flex-col" aria-label={`${name} fallbacks`}>
        {rest.map((c, k) => {
          const i = own.indexOf(c.id);
          const mine = !!learned && i === 0;
          return (
            <li key={c.id} className="flex items-center gap-s1 border-t border-line py-1.5 text-sm first:border-t-0" data-choice={c.id}>
              <span className="w-3.5 text-right text-xs text-ink-3">{k + 2}</span>
              <span className={`flex min-w-0 items-center gap-1.5 ${c.ok ? "" : "text-ink-3"}`}>
                <ChoiceName c={c} />
              </span>
              <CostTag cost={c.cost} />
              {!c.ok && <span className="text-xs text-ink-3">skipped: {c.why}</span>}
              {problems(c)}
              <span className="flex-1" />
              {mine ? (
                <span className={`${pill} text-ink-3`}>your pick</span>
              ) : (
                <span className="flex items-center">
                  <button type="button" className={quiet} aria-label={`Move ${c.label} earlier`} disabled={busy || i <= 1} onClick={() => put({ fallbacks: move(own, i, i - 1).slice(1) })}>
                    ↑
                  </button>
                  <button type="button" className={quiet} aria-label={`Move ${c.label} later`} disabled={busy || i >= own.length - 1} onClick={() => put({ fallbacks: move(own, i, i + 1).slice(1) })}>
                    ↓
                  </button>
                  <button type="button" className={quiet} aria-label={`Remove ${c.label}`} disabled={busy} onClick={() => put({ fallbacks: own.filter((x) => x !== c.id).slice(1) })}>
                    ✕
                  </button>
                </span>
              )}
            </li>
          );
        })}
        {rest.length === 0 && <li className="py-1.5 text-sm text-ink-3">No fallbacks yet.</li>}
        <li>
          <button type="button" className={`${quiet} -ml-s1`} aria-expanded={adding} onClick={onAdding}>
            {adding ? "−" : "+"} Add a fallback
          </button>
        </li>
      </ol>
      {adding && <Choices name={`${name} fallbacks`} choices={choices} skip={runOrder(state)} missing={missing} onPick={(id) => put({ fallbacks: [...own.slice(1), id] })} onNavigate={onNavigate} />}
    </div>
  );

  const edit = (
    <div className="flex flex-col gap-s1 py-s1" data-testid="edit">
      <div className="flex flex-wrap items-center gap-s2 text-sm">
        <span className="w-24 text-ink-3">Learning</span>
        <button
          type="button"
          className={`btn btn-quiet ${state.locked ? "bg-surface-2 text-ink" : ""}`}
          aria-pressed={state.locked}
          data-testid="lock"
          disabled={busy}
          onClick={() => put({ locked: !state.locked })}
        >
          {state.locked ? "Locked to your order" : "Lock to your order"}
        </button>
        <span className="text-xs text-ink-3">{state.locked ? "Learning won't change this size." : "brindle's hosted learning may run a better model here, and says so."}</span>
      </div>
      <div className="flex flex-wrap items-center gap-s2 text-sm">
        <span className="w-24 text-ink-3">Effort</span>
        <Seg name={`${name} effort`} options={EFFORT} value={(state.effort as (typeof EFFORT)[number]) ?? null} disabled={busy} onPick={(effort) => put({ effort })} />
      </div>
      <label className="flex flex-wrap items-start gap-s2 text-sm">
        <span className="w-24 text-ink-3">Instructions</span>
        <textarea
          className={`${field} min-h-16 min-w-0 flex-1`}
          value={instr}
          placeholder={`Added to whatever runs ${name} jobs.`}
          onChange={(e) => setInstr(e.target.value)}
          onBlur={() => {
            // Blank clears them: null, not "", so the engine drops the instructions.
            const next = instr.trim() ? instr : null;
            if (next !== (state.instructions || null)) put({ instructions: next });
          }}
        />
      </label>
    </div>
  );

  const picking = (
    <div className="flex flex-col gap-0.5">
      {learned && <p className="text-xs text-ink-3">Learning runs {learned.label} here now. Picking one locks this size, so learning won't change it.</p>}
      <Choices
        name={name}
        choices={choices}
        current={state.model}
        missing={missing}
        onPick={(id) => {
          onPanel(null);
          // What ran it before becomes its first fallback.
          put({ model: id, fallbacks: own.filter((x) => x !== id) });
        }}
        onNavigate={onNavigate}
      />
    </div>
  );

  const ext = panel === "pick" ? picking : panel === "fb" ? fallbacks : panel === "edit" ? edit : null;
  return (
    <li className="border-t border-line py-[11px]" aria-label={`${name} jobs`} data-testid={`kind-${size}`}>
      <div className="grid grid-cols-[9.75rem_minmax(0,18.75rem)_minmax(0,1fr)_auto] items-start gap-x-s3">
        <div>
          <div className="text-sm font-semibold">{name}</div>
          <div className="text-xs text-ink-2">{blurb}</div>
        </div>
        <div className="flex min-w-0 flex-col items-start">
          {shown ? (
            <button
              type="button"
              className={`-mx-1 flex max-w-full items-center gap-1.5 whitespace-nowrap rounded-md px-1 text-left text-sm hover:bg-surface-2 ${shown.ok ? "" : "text-ink-3"}`}
              aria-label={`${name} runs on`}
              aria-expanded={panel === "pick"}
              data-choice={shown.id}
              data-standing-in={standingIn || undefined}
              onClick={() => toggle("pick")}
            >
              <ChoiceName c={shown} />
              {learned && <span className={`${pill} text-branch`} data-learned>learned</span>}
              {standingIn && <span className={`${pill} text-pass`}>standing in</span>}
            </button>
          ) : (
            <button type="button" className={button} aria-label={`${name} runs on`} aria-expanded={panel === "pick"} onClick={() => toggle("pick")}>
              Pick a model
            </button>
          )}
          {standingIn ? (
            <span className="pl-[22px] text-xs text-ink-3" data-testid="first-pick">
              Your first pick, {first.label}, <span className="text-fail" title={first.why}>{cantRun(first)}</span>
            </span>
          ) : shown && !shown.ok ? (
            <span className="pl-[22px] text-xs text-fail" data-testid="nothing-runs">
              {shown.why}. Nothing in this order can run: these jobs wait.
            </span>
          ) : (
            shown && <MadeBy c={shown} indent />
          )}
          {learned && (
            <span className="pl-[22px] text-xs text-ink-3" data-testid="learned">
              brindle learned it does better here ·{" "}
              <button type="button" className={link} disabled={busy} onClick={() => put({ locked: true })}>
                use mine instead
              </button>
            </span>
          )}
          {first && <span className="pl-[22px]">{problems(first)}</span>}
        </div>
        <button type="button" className="min-w-0 text-left" aria-expanded={panel === "fb"} aria-label={`${name}: then, at its limit`} data-testid="fallbacks" onClick={() => toggle("fb")}>
          <span className="flex flex-wrap gap-x-5 gap-y-1.5 pt-px text-sm" data-testid="chain">
            {chain.length === 0 ? (
              <span className="text-ink-3">The task waits</span>
            ) : (
              chain.map(({ c, at }) => (
                <span key={c.id} className={`inline-flex items-center gap-1.5 whitespace-nowrap ${c.ok ? "" : "text-ink-3"}`} data-chain={c.id} data-skipped={c.ok ? undefined : c.blocker ?? "missing"}>
                  <span className="inline-block w-3.5 text-right text-xs text-ink-3">{at}</span>
                  <Mark provider={c.provider} />
                  <span data-chain-label>{c.label}</span>
                  <EffortChip effort={c.effort} />
                  {!c.ok && <span className={`${pill} text-sap`}>{shortWhy(c)}</span>}
                </span>
              ))
            )}
          </span>
        </button>
        <button type="button" className={quiet} aria-expanded={panel === "edit"} aria-label={`Edit ${name}`} onClick={() => toggle("edit")}>
          Edit
        </button>
      </div>
      {(ext || broken) && (
        <div className="mt-s1 flex flex-col gap-s1 rounded-md bg-surface-2 px-s2 py-s1">
          {ext}
          {broken && <Problems c={broken} />}
        </div>
      )}
    </li>
  );
}

/* An agent's own models by size: each size follows the job size, or runs
   a model of its own, picked inline. */
function SizeModels({ who, own, choicesFor, info, missing, busy, onSet, onNavigate }: {
  who: string;
  own: Partial<Record<Size, string>>;
  choicesFor(size: Size): Choice[];
  info(id: string): Choice;
  missing: string[];
  busy: boolean;
  onSet(size: Size, id: string | null): void;
  onNavigate?: AgentsScreenProps["onNavigate"];
}) {
  const [open, setOpen] = useState<Size | null>(null);
  return (
    <ul className="flex flex-col" aria-label={`${who}'s models by job size`}>
      {SIZES.map((s) => {
        const id = own[s];
        return (
          <li key={s} className="flex flex-col border-t border-line py-1 first:border-t-0" data-size={s} data-own={id ?? undefined}>
            <span className="flex items-center gap-s2 text-sm">
              <span className="w-20 text-ink-2">{KIND_INFO[s].name}</span>
              <button type="button" className="flex items-center gap-1.5 rounded-md px-1 hover:bg-canvas" aria-expanded={open === s} aria-label={`${who} ${KIND_INFO[s].name} model`} disabled={busy} onClick={() => setOpen(open === s ? null : s)}>
                {id ? <ChoiceName c={info(id)} /> : <span className="text-ink-2">Follows the job size</span>}
                <span className="text-ink-3" aria-hidden>
                  ▾
                </span>
              </button>
            </span>
            {open === s && (
              <Choices
                name={`${who} ${KIND_INFO[s].name}`}
                choices={choicesFor(s)}
                current={id ?? null}
                missing={missing}
                onNavigate={onNavigate}
                onPick={(pick) => {
                  setOpen(null);
                  onSet(s, pick);
                }}
                extra={
                  id ? (
                    <button
                      type="button"
                      className="rounded-md px-s1 py-0.5 text-left text-sm text-ink-2 hover:bg-canvas"
                      onClick={() => {
                        setOpen(null);
                        onSet(s, null);
                      }}
                    >
                      Follow the job size
                    </button>
                  ) : null
                }
              />
            )}
          </li>
        );
      })}
    </ul>
  );
}

const worksLikeLabel = (w: WorksLike) => agentLabel(w);

/* The add-an-agent form (and the same form to edit one of the person's
   own): one column, Name, What it's for, Instructions, Rules, then the two
   defaults behind Change. Soft-filled inputs on the page ground. */
function AgentForm({ editing, created, loading, roleUnknown, draft, setDraft, rules, taken, pinned, busy, error, choicesFor, info, missing, onSave, onCancel, onNavigate }: {
  /* The agent being edited, or null for a new one. */
  editing: AgentRow | null;
  /* A new agent whose profile is written but whose own models aren't yet: Save retries only those. */
  created: boolean;
  /* Its saved instructions and role are being read: the form waits for them. */
  loading: boolean;
  /* The read failed: its saved role isn't known, so its role and models can't be changed here. */
  roleUnknown: boolean;
  draft: Draft;
  setDraft(d: Draft): void;
  rules: RulePackInfo[];
  taken: string[];
  /* A pinned profile's model ("Its own"), when editing one. */
  pinned: Choice | null;
  busy: boolean;
  error: string | null;
  /* The models an agent that works like this may keep as its own for a size. */
  choicesFor(worksLike: WorksLike, size: Size): Choice[];
  info(id: string): Choice;
  missing: string[];
  onSave(): void;
  onCancel(): void;
  onNavigate?: AgentsScreenProps["onNavigate"];
}) {
  const set = (patch: Partial<Draft>) => setDraft({ ...draft, ...patch });
  const ownSizes = SIZES.filter((s) => draft.own[s]);
  const [change, setChange] = useState(false);
  const [ownModel, setOwnModel] = useState(ownSizes.length > 0);
  const problem = editing || created ? (draft.purpose.trim() ? null : "Say what it's for.") : draftProblem(draft, taken);
  /* A new role keeps only the own models that role may run. */
  const pickRole = (worksLike: WorksLike) => {
    const own: Partial<Record<Size, string>> = {};
    for (const s of SIZES) {
      const id = draft.own[s];
      if (id && choicesFor(worksLike, s).some((c) => c.id === id && c.ok)) own[s] = id;
    }
    set({ worksLike, own });
  };
  const mode = ownSizes.length === 0 ? "follows" : ownSizes.length === SIZES.length ? "own" : "except";
  const modelSummary = mode === "follows" ? "follows the job size" : mode === "own" ? "is its own for every size" : `follows the job size, except ${ownSizes.map((s) => KIND_INFO[s].name).join(", ")}`;
  const first = useRef<HTMLInputElement>(null);
  useEffect(() => first.current?.focus(), []);
  const hint = "text-xs text-ink-3";
  const key = "text-[13px] font-medium";
  const title = editing ? `Edit ${editing.label}` : "New agent";
  return (
    <section className="border-t border-line py-s3" aria-label={title} data-testid="agent-form">
      <div className="flex max-w-[620px] flex-col gap-[18px]">
        <h3 className={h2}>{title}</h3>
        <label className="flex flex-col gap-[5px]">
          <span className={key}>Name</span>
          <input
            ref={editing ? undefined : first}
            className={`${softField} ${mono}`}
            value={draft.name}
            placeholder="frontend-developer"
            readOnly={!!editing || created}
            aria-readonly={!!editing || created || undefined}
            onChange={(e) => set({ name: e.target.value })}
          />
        </label>
        <div className="flex flex-col gap-[5px]">
          <label className="flex flex-col gap-[5px]">
            <span className={key}>What it's for</span>
            <input ref={editing ? first : undefined} className={softField} value={draft.purpose} placeholder="Builds and polishes UI components" onChange={(e) => set({ purpose: e.target.value })} />
          </label>
          <span className={hint}>One line. The supervisor reads it to decide which tasks this agent gets.</span>
        </div>
        <label className="flex flex-col gap-[5px]">
          <span className={key}>Instructions</span>
          <textarea
            className={`${softField} min-h-[92px] resize-y`}
            value={draft.instructions}
            readOnly={loading}
            aria-busy={loading || undefined}
            placeholder={loading ? "Reading its instructions…" : "How it should work, in your words. For example: use the app's tokens, and render the screen before you report."}
            onChange={(e) => set({ instructions: e.target.value })}
          />
        </label>
        <div className="flex flex-col gap-[5px]" role="group" aria-label="Rules">
          <span className={key}>Rules</span>
          <ul className="-mx-2 flex flex-col gap-0.5">
            {rules.map((r) => (
              <li key={r.name}>
                <label className="grid cursor-pointer grid-cols-[14px_minmax(0,1fr)] gap-x-2.5 rounded-[6px] px-2 py-[5px] hover:bg-surface-2" data-rule={r.name}>
                  <span className="relative mt-[3px] flex">
                    <input
                      type="checkbox"
                      className="peer sr-only"
                      checked={draft.rules.includes(r.name)}
                      onChange={() => set({ rules: draft.rules.includes(r.name) ? draft.rules.filter((x) => x !== r.name) : [...draft.rules, r.name] })}
                    />
                    <span className={softCheck} aria-hidden>
                      ✓
                    </span>
                  </span>
                  <span className={mono}>{r.name}</span>
                  <span className="col-start-2 text-xs text-ink-3">{r.description}</span>
                </label>
              </li>
            ))}
            {rules.length === 0 && <li className={`${hint} px-2`}>No rule packs here yet.</li>}
          </ul>
          <span className={hint}>
            Ready-made instructions it must follow. Tick any that apply.{" "}
            <button type="button" className={link} onClick={() => onNavigate?.("knowledge")}>
              See all rules
            </button>
          </span>
        </div>
        <div className="flex flex-col gap-3 border-t border-line pt-3.5">
          <p className="text-[13px] text-ink-2" data-testid="defaults" data-works-like={draft.worksLike} data-model-mode={pinned ? "own" : mode}>
            {pinned ? (
              <>Runs its own model, {pinned.display}, whatever the job size.</>
            ) : roleUnknown ? (
              <span data-testid="role-unknown">
                Its model {modelSummary}. What it works like couldn't be read, so its role and models can't be changed here now.
              </span>
            ) : (
              <>
                Works like a {worksLikeLabel(draft.worksLike)}, and its model {modelSummary}.{" "}
                {/* Its saved role decides which models it may keep: nothing to change until that's read. */}
                <button type="button" className={`${link} disabled:opacity-[var(--dim)]`} aria-expanded={change && !loading} disabled={loading} onClick={() => setChange(!change)}>
                  Change
                </button>
              </>
            )}
          </p>
          {change && !pinned && !loading && !roleUnknown && (
            <div className="flex flex-col gap-3" data-testid="change">
              <div className="flex flex-wrap items-center gap-s1">
                <span className="w-[110px] text-sm text-ink-2">Works like</span>
                <Seg name="Works like" options={WORKS_LIKE} value={draft.worksLike} labelOf={worksLikeLabel} disabled={loading} onPick={pickRole} />
                <span className="text-xs text-ink-3">Its tools and what it may do.</span>
              </div>
              <div className="flex flex-wrap items-center gap-s1">
                <span className="w-[110px] text-sm text-ink-2">Model</span>
                <Seg<"follows" | "own">
                  name="Model"
                  options={["follows", "own"]}
                  value={ownModel ? "own" : "follows"}
                  labelOf={(m) => (m === "follows" ? "Follows the job size" : "Its own for a size…")}
                  onPick={(m) => {
                    setOwnModel(m === "own");
                    if (m === "follows") set({ own: {} });
                  }}
                />
              </div>
              {ownModel && (
                <div className="pl-[118px]">
                  <SizeModels
                    who={cleanName(draft.name) || "It"}
                    own={draft.own}
                    choicesFor={(s) => choicesFor(draft.worksLike, s)}
                    info={info}
                    missing={missing}
                    busy={busy}
                    onSet={(s, id) => {
                      const next = { ...draft.own };
                      if (id) next[s] = id;
                      else delete next[s];
                      set({ own: next });
                    }}
                    onNavigate={onNavigate}
                  />
                </div>
              )}
            </div>
          )}
          {error && (
            <p role="alert" className={alert} data-testid="form-error">
              {created ? "It's created, but its own models couldn't be saved" : `Could not ${editing ? "save" : "create"} it`}: {withLinks(error)}
            </p>
          )}
          <div className="flex flex-wrap items-center justify-end gap-s1">
            {problem && draft.name.trim() && (
              <span className="mr-auto text-xs text-sap" data-testid="form-problem">
                {problem}
              </span>
            )}
            <button type="button" className={quiet} onClick={onCancel}>
              Cancel
            </button>
            <button type="button" className={main} disabled={!!problem || busy || loading} onClick={onSave}>
              {busy ? (editing || created ? "Saving…" : "Creating…") : editing ? "Save" : created ? "Save its models" : "Create agent"}
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}

/* The Model cell of an agent's row. */
function AgentModel({ a, info, pinned }: { a: AgentRow; info(id: string): Choice; pinned: Choice | null }) {
  const own = (id: string) => {
    const c = info(id);
    return (
      <span className="inline-flex items-center gap-1.5">
        <Mark provider={c.provider} />
        <span data-model-name={c.display}>{c.display}</span>
        <EffortChip effort={c.effort} />
      </span>
    );
  };
  if (pinned)
    return (
      <span className="flex flex-col text-sm">
        {own(pinned.id)}
        <span className="text-xs text-ink-3">Its own: it names a model</span>
      </span>
    );
  if (a.mode === "follows") return <span className="text-sm text-ink-2">Follows the job size</span>;
  const sizes = SIZES.filter((s) => a.models[s]?.model);
  const ids = [...new Set(sizes.map((s) => a.models[s]!.model!))];
  if (a.mode === "own" && ids.length === 1)
    return (
      <span className="flex flex-col text-sm">
        {own(ids[0])}
        <span className="text-xs text-ink-3">{a.name === "supervisor" ? "Its own: a session isn't sized" : "Its own for every job size"}</span>
      </span>
    );
  return (
    <span className="flex flex-col text-sm">
      <span className="text-ink-2">{a.mode === "own" ? "Its own for each job size" : "Follows the job size, except"}</span>
      {sizes.map((s) => (
        <span key={s} className="flex items-center gap-1.5">
          {KIND_INFO[s].name}: {own(a.models[s]!.model!)}
        </span>
      ))}
    </span>
  );
}

/* Until the learned picks are read again, a kind keeps the one it had,
   unless it's locked now (never overridden) or that is now its own pick. */
function keepLearned(next: RoutingDTO, prev: RoutingDTO | null): RoutingDTO {
  if (!next.kinds || !prev?.kinds) return next;
  const kinds = Object.fromEntries(
    Object.entries(next.kinds).map(([k, v]) => {
      const was = prev.kinds?.[k]?.learned;
      return [k, !v.learned && was && !v.locked && was !== v.model ? { ...v, learned: was } : v];
    }),
  );
  return { ...next, kinds };
}

/* One thing that stops a size's first pick, with what fixes it. */
interface Need {
  key: string;
  tone: "fail" | "sap";
  head: string;
  body: string;
  action: ReactNode;
}

interface FormState {
  /* This opening of the form: an answer to a request it made applies only while it is still the one open. */
  id: number;
  /* The agent being edited, or null for a new one. */
  editing: string | null;
  /* What stopped its last save. */
  error?: string | null;
  draft: Draft;
  /* A new agent's profile is written (POST /profiles went through): saving again writes only its own models. */
  created?: boolean;
  /* Reading an edited agent's saved instructions and role. */
  loading?: boolean;
  /* That read failed: its role and models stay as saved. */
  roleUnknown?: boolean;
  /* What was saved when it opened, to send only what this form changed:
     instructions null when they couldn't be read. A later reload of the
     list never counts as a change made here. */
  base?: { instructions: string | null; worksLike: WorksLike; own: Partial<Record<Size, string>> };
}

const roleOf = (role: string | null): WorksLike => (role === "reviewer" ? "reviewer" : "developer");

const ownOf = (a: AgentRow): Partial<Record<Size, string>> => Object.fromEntries(SIZES.filter((s) => a.models[s]?.model).map((s) => [s, a.models[s]!.model!]));

export function AgentsScreen({ client, onNavigate }: AgentsScreenProps) {
  const { repos } = useSessions();
  /* Agents are the person's, the same in every repo (their settings and
     library, PUT at scope "user"): one list, whatever the repos. A repo only
     gives the engine its org policy and what learning knows. */
  const repo = repos?.[0]?.repo_root ?? null;
  const [routing, setRouting] = useState<RoutingDTO | null>(null);
  const [describe, setDescribe] = useState<Record<string, string>>({});
  const [rules, setRules] = useState<RulePackInfo[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<{ size: Size; panel: Panel } | null>(null);
  const [adding, setAdding] = useState<Size | null>(null);
  /* The row and the agent whose problems are open there. */
  const [probs, setProbs] = useState<{ size: Size; id: string } | null>(null);
  /* The form: a new agent (editing null) or the one being edited, and its answers. */
  const [form, setForm] = useState<FormState | null>(null);
  const forms = useRef(0);
  /* A change for the form opening `id`, dropped when another opening (or none) is up now. */
  const updateForm = (id: number, change: (f: FormState) => FormState | null) => setForm((f) => (f?.id === id ? change(f) : f));
  /* A built-in agent whose own models are open under its row. */
  const [models, setModels] = useState<string | null>(null);
  const [fresh, setFresh] = useState<string | null>(null);
  const seq = useRef(0);

  const load = useCallback(() => {
    if (!repo) return Promise.resolve();
    const mine = ++seq.current;
    // The rows first: what learning runs instead resolves the dispatch for
    // every candidate (seconds), so it fills in after, and only the newest load's.
    return Promise.all([client.routing(repo, { scope: "user", learned: false, includeRepoOrders: true }), client.profiles().catch(() => []), client.rulePacks(repo).catch(() => [])]).then(
      ([r, ps, rs]) => {
        if (mine !== seq.current) return;
        setRouting((prev) => keepLearned(r, prev));
        setDescribe(Object.fromEntries(ps.map((p) => [p.name, p.description])));
        setRules(rs);
        setLoadError(null);
        client.routing(repo, { scope: "user", includeRepoOrders: true }).then(
          (full) => mine === seq.current && setRouting(full),
          () => {}, // the rows stand without what learning runs instead
        );
      },
      (e: unknown) => {
        if (mine === seq.current) setLoadError(message(e));
      },
    );
  }, [client, repo]);

  useEffect(() => {
    setRouting(null);
    setOpen(null);
    setForm(null);
    setBusy(false);
    setSaveError(null);
    void load();
  }, [load]);

  const profiles = useMemo(() => routing?.profiles ?? [], [routing]);
  const connected = useMemo(() => connectedProviders(profiles), [profiles]);
  const kinds = useMemo(() => (routing ? kindsOf(routing) : null), [routing]);
  const agents = useMemo(() => (routing ? agentRows(routing) : []), [routing]);
  const byName = useMemo(() => new Map(profiles.map((p) => [p.name, p])), [profiles]);
  const info = useCallback(
    (id: string): Choice => {
      const p = byName.get(id);
      return p ? choiceOf(p, connected, describe) : missingChoice(id);
    },
    [byName, connected, describe],
  );
  const policy = routing?.policy;
  const frozen = !!policy?.applies && policy.editable === false;
  const differences = SIZES.flatMap((size) => {
    const orders = routing?.orders?.[size];
    if (!orders) return [];
    const { recommended, saved } = orders;
    return saved
      .filter(({ order }) => order.length !== recommended.length || order.some((name, i) => name !== recommended[i]))
      .map((entry) => ({ size, ...entry }));
  });
  const missing = providersOf(profiles).filter((p) => !connected.has(p) && p !== "native");
  const offered = (k: Kind) => offeredFor(k, profiles).map((p) => choiceOf(p, connected, describe));
  const pinnedOf = (a: AgentRow) => (a.pinned ? info(a.name) : null);

  const save = (body: Omit<RoutingPut, "repo" | "scope">) => {
    if (!repo || frozen) return Promise.resolve();
    setBusy(true);
    setSaveError(null);
    return client
      .putRouting({ repo, scope: "user", ...body })
      .then(load)
      .catch((e: unknown) => setSaveError(message(e)))
      .finally(() => setBusy(false));
  };
  const put = (size: Size, change: KindPut) => save({ kinds: { [size]: change } });

  const resetOrders = async () => {
    if (!repo || frozen || busy) return;
    const byScope = new Map<RoutingScope, NonNullable<RoutingPut["clear_orders"]>>();
    for (const { scope, size, setting, agent, order } of differences) {
      const orders = byScope.get(scope) ?? {};
      (orders[size] ??= []).push({ setting, agent, order });
      byScope.set(scope, orders);
    }
    const started = seq.current;
    setBusy(true);
    setSaveError(null);
    const results = await Promise.allSettled(
      Array.from(byScope, ([scope, clear_orders]) => client.putRouting({ repo, scope, clear_orders })),
    );
    if (started !== seq.current) return;
    const failed = results.find((result) => result.status === "rejected");
    if (failed) setSaveError(message(failed.reason));
    const reload = load();
    const reloading = seq.current;
    await reload;
    if (reloading === seq.current) setBusy(false);
  };

  /* An agent's own order for a size: the model picked, then the shared
     table's order behind it, less what the org refuses or no longer exists
     (the engine checks every name it's given). */
  const orderFor = (size: Size, id: string) => ({
    model: id,
    fallbacks: kinds ? runOrder(kinds[size]).filter((x) => x !== id && byName.has(x) && byName.get(x)!.allowed !== false) : [],
  });
  const ownChanges = (before: Partial<Record<Size, string>>, after: Partial<Record<Size, string>>) => {
    const out: Record<string, { model: string; fallbacks?: string[] } | null> = {};
    for (const s of SIZES) {
      if (before[s] === after[s]) continue;
      out[s] = after[s] ? orderFor(s, after[s]!) : null;
    }
    return out;
  };
  /* What an agent's own model for a size may be: the models its role runs. */
  const choicesFor = (role: string | null) => (s: Size) => offered(role === "reviewer" ? "reviewer" : s);
  const formChoices = (worksLike: WorksLike, s: Size) => choicesFor(worksLike)(s);

  const panelFor = (s: Size): Panel | null => (open?.size === s ? open.panel : null);
  const setPanel = (s: Size, p: Panel | null) => {
    setOpen(p ? { size: s, panel: p } : null);
    setAdding(null);
  };

  const startNew = () => {
    setForm({ id: ++forms.current, editing: null, draft: newDraft() });
    setModels(null);
  };
  const startEdit = (a: AgentRow) => {
    if (!a.custom) {
      setForm(null);
      setModels(models === a.name ? null : a.name);
      return;
    }
    setModels(null);
    if (form?.editing === a.name) return setForm(null);
    const fallback = roleOf(a.role);
    const id = ++forms.current;
    setForm({
      id,
      editing: a.name,
      loading: true,
      draft: { name: a.name, purpose: a.description, instructions: "", rules: a.rules, worksLike: fallback, own: ownOf(a) },
      base: { instructions: null, worksLike: fallback, own: ownOf(a) },
    });
    // Its own instructions and what it works like, as saved; only this opening's answer is used.
    client.profile(a.name).then(
      (d) => {
        const like = (WORKS_LIKE as string[]).includes(d.extends?.[0] ?? "") ? (d.extends[0] as WorksLike) : fallback;
        const saved = typeof d.instructions === "string" ? d.instructions : null;
        updateForm(id, (f) => ({ ...f, loading: false, draft: { ...f.draft, instructions: saved ?? "", worksLike: like }, base: { ...f.base!, instructions: saved, worksLike: like } }));
      },
      // Not read: what it works like stays unknown, so its role and models aren't offered (only new instructions go).
      () => updateForm(id, (f) => ({ ...f, loading: false, roleUnknown: true })),
    );
  };

  const submit = async () => {
    if (!repo || !form) return;
    const { id, draft, editing, base } = form;
    // Its role wasn't read: its role and models were never offered, so neither is written.
    const roleAndModels = !form.roleUnknown;
    let created = !!form.created;
    const name = editing ?? cleanName(draft.name);
    const before = editing ? agents.find((a) => a.name === editing) : undefined;
    setBusy(true);
    updateForm(id, (f) => ({ ...f, error: null }));
    try {
      if (!editing && !created) {
        // An agent is how to work: no provider, model or effort; its model follows the job size.
        await client.createProfile({ repo, name, description: draft.purpose.trim(), works_like: draft.worksLike, instructions: draft.instructions, rules: draft.rules });
        // Written: from here a second Save of this form writes only its own models, and the list shows it.
        created = true;
        updateForm(id, (f) => ({ ...f, created: true }));
      } else if (editing) {
        // Instructions go when they changed: "" clears saved ones. When they couldn't be read, only new text goes.
        const instructions = base?.instructions == null ? (draft.instructions.trim() ? draft.instructions : undefined) : draft.instructions !== base.instructions ? draft.instructions : undefined;
        await client.updateProfile(name, {
          repo,
          description: draft.purpose.trim(),
          rules: draft.rules,
          ...(instructions !== undefined ? { instructions } : {}),
          ...(roleAndModels && !before?.pinned && base && draft.worksLike !== base.worksLike ? { works_like: draft.worksLike } : {}),
        });
      }
      // Only the sizes this form changed, against what it opened with: a newer override read since stays.
      const changes = !roleAndModels || before?.pinned ? {} : ownChanges(base?.own ?? {}, draft.own);
      if (Object.keys(changes).length > 0) await client.putRouting({ repo, scope: "user", agents: { [name]: { models: changes } } });
      setFresh(name);
      updateForm(id, () => null);
      await load();
    } catch (e) {
      updateForm(id, (f) => ({ ...f, error: message(e) }));
      // It was written before what failed: the list shows it now.
      if (created) void load();
    } finally {
      setBusy(false);
    }
  };

  /* Needs you: per size, a first pick that can't run, with its one fix. */
  const needs: Need[] = [];
  if (kinds) {
    const offline = new Map<string, { names: Set<string>; sizes: Set<string>; blocker: string }>();
    for (const s of SIZES) {
      const list = runOrder(kinds[s]).map(info);
      const [first] = list;
      const now = runner(list);
      for (const c of list) {
        if (c.blocker !== "signed_out" && c.blocker !== "not_installed") continue;
        const got = offline.get(c.provider) ?? { names: new Set(), sizes: new Set(), blocker: c.blocker };
        got.names.add(c.label);
        got.sizes.add(KIND_INFO[s].name);
        offline.set(c.provider, got);
      }
      if (!first || first.ok || first.blocker === "signed_out" || first.blocker === "not_installed") continue;
      needs.push({
        key: `size-${s}`,
        tone: "fail",
        head: `${first.label} ${cantRun(first)}.`,
        body: now ? `${KIND_INFO[s].name} jobs run on ${now.label} instead.` : `Nothing can run ${KIND_INFO[s].name} jobs now: they wait.`,
        action: now && (
          <button type="button" className={quiet} disabled={busy || frozen} onClick={() => void put(s, { model: now.id, fallbacks: runOrder(kinds[s]).filter((x) => x !== now.id) })}>
            Put {now.label} first
          </button>
        ),
      });
    }
    for (const [p, { names, sizes, blocker }] of offline) {
      const signedOut = blocker === "signed_out";
      needs.push({
        key: `provider-${p}`,
        tone: "sap",
        head: `${productName(p)} ${signedOut ? "is signed out" : "isn't connected"}.`,
        body: `${[...names].join(", ")} ${names.size === 1 ? "is" : "are"} skipped in ${[...sizes].join(", ")}.`,
        action: (
          <button type="button" className={quiet} data-connect={p} onClick={() => onNavigate?.("providers")}>
            {signedOut ? "Sign in" : `Connect ${productName(p)}`}
          </button>
        ),
      });
    }
  }

  /* A provider that isn't connected and no size uses: one quiet line at the foot. */
  const quietMissing = missing.filter((p) => !needs.some((n) => n.key === `provider-${p}`));

  const formFor = (editing: AgentRow | null) =>
    form && (
      <AgentForm
        editing={editing}
        created={!!form.created}
        loading={!!form.loading}
        roleUnknown={!!form.roleUnknown}
        draft={form.draft}
        setDraft={(draft) => setForm((f) => f && { ...f, draft })}
        rules={rules}
        taken={profiles.map((p) => p.name)}
        pinned={editing ? pinnedOf(editing) : null}
        busy={busy}
        error={form.error ?? null}
        choicesFor={formChoices}
        info={info}
        missing={missing}
        onSave={() => void submit()}
        onCancel={() => setForm(null)}
        onNavigate={onNavigate}
      />
    );

  const head = "grid grid-cols-[12.5rem_minmax(0,1.25fr)_minmax(0,0.9fr)_minmax(0,1.05fr)_3rem] items-start gap-x-s3";
  return (
    <section className="flex flex-col" aria-label="Agents">
      <div className="pb-s2">
        <PageHeader title="Agents" lede="Two choices, kept apart: who does a task, and what model runs it.">
          {kinds && (
            <button type="button" className={button} aria-expanded={form?.editing === null} onClick={() => (form?.editing === null ? setForm(null) : startNew())}>
              New agent
            </button>
          )}
        </PageHeader>
      </div>

      {repos && repos.length === 0 && <p className="text-ink-3">{NO_REPO}</p>}
      {loadError && (
        <div role="alert" className={`${alert} flex items-center justify-between gap-s2`}>
          <span>Could not load agents: {loadError}</span>
          <button type="button" className={button} onClick={() => void load()}>
            Retry
          </button>
        </div>
      )}
      {repo && !routing && !loadError && <p className="text-ink-3">Loading…</p>}
      {saveError && (
        <p role="alert" className={`${alert} mb-s1`}>
          Could not save: {withLinks(saveError)}
        </p>
      )}

      {kinds && needs.length > 0 && (
        <section className={`${card} pb-1.5 pt-2.5`} aria-label="Needs you" data-testid="needs-you">
          <div className="flex items-center gap-s1">
            <span className={label}>Needs you</span>
            <span className="text-xs text-ink-3">{needs.length}</span>
          </div>
          <ul>
            {needs.map((n) => (
              <li key={n.key} className="flex items-center gap-2.5 py-1.5 text-sm" data-need={n.key}>
                <span className={`${pill} ${n.tone === "fail" ? "text-fail" : "text-sap"}`} aria-hidden />
                <span>
                  <span className="font-medium">{n.head}</span> <span className="text-ink-2">{n.body}</span>
                </span>
                <span className="flex-1" />
                {n.action}
              </li>
            ))}
          </ul>
        </section>
      )}

      {kinds && (
        <section className={`${card} pt-3.5`} aria-labelledby="who-title">
          <div className="flex flex-wrap items-baseline gap-x-s1 pb-3">
            <h2 id="who-title" className={h2}>
              Who does the work
            </h2>
            <span className="text-sm text-ink-2">The supervisor gives each task to the agent that fits it. An agent is how to work, not which model.</span>
          </div>
          {form?.editing === null && formFor(null)}
          <div className={`${head} pb-2`} aria-hidden>
            <span className={label}>Agent</span>
            <span className={label}>What it does</span>
            <span className={label}>Rules</span>
            <span className={label}>Model</span>
            <span />
          </div>
          <ul aria-label="Agents">
            {agents.map((a) => {
              const pinned = pinnedOf(a);
              return (
                <li key={a.name} data-testid={`agent-${a.name}`} data-model-mode={a.mode} data-custom={a.custom || undefined} data-fresh={fresh === a.name || undefined}>
                  <div className={`${head} border-t border-line py-2.5`}>
                    <span className="flex min-w-0 items-center gap-s1">
                      <span className={`font-medium ${a.custom ? mono : "text-sm"}`}>{a.label}</span>
                      {a.custom && <span className="rounded-sm border border-line px-[5px] text-[11px] leading-[15px] text-ink-3">yours</span>}
                    </span>
                    <span className="text-[13px] text-ink-2">{a.description}</span>
                    {a.rules.length > 0 ? (
                      <span className={`${mono} flex flex-col`}>
                        {a.rules.map((r) => (
                          <span key={r}>{r}</span>
                        ))}
                      </span>
                    ) : (
                      <span className="text-[13px] text-ink-3">None</span>
                    )}
                    <AgentModel a={a} info={info} pinned={pinned} />
                    <button type="button" className={quiet} aria-label={`Edit ${a.label}`} aria-expanded={form?.editing === a.name || models === a.name} disabled={frozen && !a.custom} onClick={() => startEdit(a)}>
                      Edit
                    </button>
                  </div>
                  {form?.editing === a.name && formFor(a)}
                  {models === a.name && (
                    <div className="mb-s1 flex flex-col gap-s1 rounded-md bg-surface-2 px-s2 py-s1" data-testid="agent-models">
                      <p className="text-xs text-ink-2">
                        {a.label} is built in: its instructions and rules come with brindle. Its model follows the job size, unless you give it its own for a size.
                      </p>
                      <SizeModels
                        who={a.label}
                        own={ownOf(a)}
                        choicesFor={choicesFor(a.role)}
                        info={info}
                        missing={missing}
                        busy={busy || frozen}
                        onSet={(s, id) => void save({ agents: { [a.name]: { models: { [s]: id ? orderFor(s, id) : null } } } })}
                        onNavigate={onNavigate}
                      />
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {kinds && (
        <section className={`${card} mt-1 pt-3.5`} aria-labelledby="model-title">
          <div className="flex flex-wrap items-baseline gap-x-s1 pb-3">
            <h2 id="model-title" className={h2}>
              What model runs it
            </h2>
            <span className="text-sm text-ink-2">By job size, for every agent that follows it. When the first is at its limit, the next takes over.</span>
          </div>
          {differences.length > 0 && (
            <p className="pb-3 text-xs text-ink-3" data-testid="routing-differs">
              {SIZES.map((size) => (
                <Fragment key={size}>
                  {differences.some((entry) => entry.size === size && entry.locked) && (
                    <span data-size={size} data-routing-notice="own-order">
                      {KIND_INFO[size].name}: your own order (you chose it over the learned pick).{" "}
                    </span>
                  )}
                  {differences.some((entry) => entry.size === size && !entry.locked) && (
                    <span data-size={size} data-routing-notice="differs">
                      Your routing differs from brindle's recommended order ({KIND_INFO[size].name}).{" "}
                    </span>
                  )}
                </Fragment>
              ))}
              <button type="button" className={link} disabled={busy || frozen} onClick={() => void resetOrders()}>
                Reset to recommended
              </button>
            </p>
          )}
          <div className="grid grid-cols-[9.75rem_minmax(0,18.75rem)_minmax(0,1fr)_auto] gap-x-s3 pb-2" aria-hidden>
            <span className={label}>Job size</span>
            <span className={label}>Runs it</span>
            <span className={label}>Then, at its limit</span>
            <span />
          </div>
          <ul aria-label="Job sizes">
            {SIZES.map((s) => (
              <SizeRow
                key={s}
                size={s}
                state={kinds[s]}
                choiceOf={info}
                choices={offered(s)}
                missing={missing}
                panel={panelFor(s)}
                adding={adding === s}
                probs={probs?.size === s ? probs.id : null}
                busy={busy || frozen}
                onPanel={(p) => setPanel(s, p)}
                onAdding={() => setAdding(adding === s ? null : s)}
                onProbs={(id) => setProbs(probs?.size === s && probs.id === id ? null : { size: s, id })}
                put={(change) => void put(s, change)}
                onNavigate={onNavigate}
              />
            ))}
          </ul>
        </section>
      )}

      {kinds && (policy?.applies || quietMissing.length > 0) && (
        <p className="mt-1.5 flex flex-wrap items-center gap-1.5 border-t border-line pt-2.5 text-xs text-ink-3" data-testid="policy">
          <svg className="size-[13px] shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <path d="M7 11V8a5 5 0 0 1 10 0v3M6 11h12v9H6z" />
          </svg>
          {policy?.applies && <span>Your org's policy applies: models it doesn't allow aren't offered.{frozen && ` Nothing here can change now: ${policy.reason ?? "the policy couldn't be read"}.`}</span>}
          {quietMissing.map((p) => (
            <span key={p} className="flex items-center gap-1" data-missing={p}>
              {productName(p)} isn't connected.
              <ConnectLink provider={p} onNavigate={onNavigate}>
                <span className="text-xs">Connect it in Settings › Providers</span>
              </ConnectLink>
            </span>
          ))}
        </p>
      )}
    </section>
  );
}
