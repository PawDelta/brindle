/* Agents: which model runs each kind of work, and what takes over at a
   limit (design/agents-2, option A, as the owner approved it). Two cards:

   - "How a task gets run": the four steps, automatic.
   - "Who does each kind of work": a row per kind (the four sizes the
     supervisor gives tasks, then the reviewer and the supervisor), each with
     what it runs on (a model, or one of the person's custom agents, picked
     from a list of the connected providers' models with cost and speed),
     "If it's at its limit: X, then Y" with the ordered fallbacks behind it
     (↑ ↓ ✕, + Add a fallback), a Lock against hosted learning, and More
     (effort and instructions); the reviewer and the supervisor are rows like
     the sizes. Whether an agent can run is its own: installed, its own
     sign-in, its provider not excluded, no problems, each with why. When learning runs something else for a
     kind, the row runs it, tagged "learned", and says only "brindle learned
     X does better here · use mine instead" (its basis stays internal);
     "use mine instead" locks the row.

   "Make a custom agent…" at the foot of a row's list opens the guided path
   under that row: what it's for → provider and model → how it works
   (effort, instructions, rules) → what it's used for → a checked summary,
   and Create. Every agent's automatic check (profile lint, in GET /routing)
   shows as "N problems" where it's used; there is no lint button.

   Nothing opens over the page: lists, panels and the builder are all inline
   under the row they change. No <select>. Every change goes straight to the
   engine (PUT /v1/routing `kinds`, into this repo's personal config) and the
   page reads GET /v1/routing again. */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { DaemonClient, KindPut, RulePackInfo } from "../../api/client";
import type { RoutingDTO, RoutingProfileDTO } from "../../api/schema";
import { plain } from "../../api/wording";
import { PageHeader } from "../../components/PageHeader";
import { NO_REPO, useRepo } from "../../components/RepoSelect";
import { alert, button, card, field, h2, label, mono, pill, primary, quiet } from "../../components/ui";
import {
  COST_MARK,
  COST_WORDS,
  EFFORT,
  FLOW,
  KINDS,
  KIND_INFO,
  SIZES,
  STEPS,
  choiceOf,
  cleanName,
  connectedProviders,
  draftProblem,
  isCustom,
  kindsOf,
  missingChoice,
  modelsOf,
  move,
  newDraft,
  offeredFor,
  prettyModel,
  problemCount,
  productName,
  providersOf,
  runOrder,
  speedOf,
  type Choice,
  type Draft,
  type Kind,
  type KindState,
} from "./agents";

export interface AgentsScreenProps {
  client: DaemonClient;
  /* Where a link on the page goes: Settings › Providers ("Connect Codex"), Knowledge's rules ("See all rules"). */
  onNavigate?(to: "providers" | "knowledge"): void;
}

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

/* What is open under a row: its list, its fallbacks, its More, or the builder. */
type Panel = "pick" | "fb" | "more" | "make";

const MARK: Record<string, string> = { claude: "bg-fawn text-on-fawn", codex: "bg-ink text-surface", opencode: "bg-ink-3 text-surface", antigravity: "bg-branch text-on-branch", native: "bg-pass text-surface" };

/* A provider's small letter mark. */
function Mark({ provider }: { provider: string }) {
  if (!provider) return null;
  return (
    <span className={`inline-flex size-4 shrink-0 items-center justify-center rounded-sm text-[10px] font-bold ${MARK[provider] ?? "bg-ink-3 text-surface"}`} aria-hidden>
      {productName(provider)[0]}
    </span>
  );
}

function CostTag({ cost }: { cost: string | null }) {
  if (!cost) return null;
  return (
    <span className={`${mono} text-ink-3`} title={COST_WORDS[cost]} aria-label={COST_WORDS[cost] ?? cost}>
      {COST_MARK[cost] ?? cost}
    </span>
  );
}

function ChoiceName({ c }: { c: Choice }) {
  return (
    <>
      <Mark provider={c.provider} />
      <span className={c.custom ? mono : ""}>{c.label}</span>
      {c.custom && c.model && <span className="text-ink-3">· {prettyModel(c.model)}</span>}
    </>
  );
}

/* A one-of-several choice as a row of buttons (never a <select>). */
function Seg<T extends string>({ name, options, value, onPick, disabled, labelOf }: { name: string; options: readonly T[]; value: T | null; onPick(v: T): void; disabled?: boolean; labelOf?(v: T): string }) {
  return (
    <span role="group" aria-label={name} className="inline-flex flex-wrap overflow-hidden rounded-md border border-line">
      {options.map((o) => (
        <button
          key={o}
          type="button"
          aria-pressed={value === o}
          disabled={disabled}
          className={`border-r border-line px-s1 py-0.5 text-sm last:border-r-0 ${value === o ? "bg-branch text-on-branch" : "text-ink-2 hover:bg-surface-2"}`}
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
    <button type="button" className="text-sm text-branch underline" data-connect={provider} onClick={() => onNavigate?.("providers")}>
      {children ?? `Connect ${productName(provider)}`}
    </button>
  );
}

/* The choices for a slot, grouped: the person's custom agents, each
   connected provider's models (cost, speed, what it's good at), then one
   quiet line per provider that isn't connected. Inline, under the row. */
function Choices({ name, choices, current, skip = [], fresh, onPick, onMake, missing, onNavigate }: {
  name: string;
  choices: Choice[];
  current?: string | null;
  skip?: string[];
  fresh?: string | null;
  onPick(id: string): void;
  onMake?(): void;
  missing: string[];
  onNavigate?: AgentsScreenProps["onNavigate"];
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
        <span className="grid w-full grid-cols-[minmax(0,14rem)_5rem_minmax(0,1fr)] items-baseline gap-x-s2 px-s1 py-0.5 text-sm text-ink-3" data-choice-off={c.id}>
          <span className="flex min-w-0 items-center gap-1">
            <ChoiceName c={c} />
          </span>
          <span className="flex items-baseline gap-1 text-xs">
            <CostTag cost={c.cost} />
            {c.speed}
          </span>
          <span className="truncate text-xs text-sap">{c.why}</span>
        </span>
      ) : (
      <button
        type="button"
        className={`grid w-full grid-cols-[minmax(0,14rem)_5rem_minmax(0,1fr)] items-baseline gap-x-s2 rounded-md px-s1 py-0.5 text-left text-sm hover:bg-surface-2 ${c.id === current ? "bg-branch-soft" : ""}`}
        aria-current={c.id === current ? "true" : undefined}
        data-choice={c.id}
        onClick={() => onPick(c.id)}
      >
        <span className="flex min-w-0 items-center gap-1">
          <ChoiceName c={c} />
          {fresh === c.id && <span className={`${pill} bg-pass-soft text-pass`}>new</span>}
        </span>
        <span className="flex items-baseline gap-1 text-xs text-ink-3">
          <CostTag cost={c.cost} />
          {c.speed}
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
    <div className="flex flex-col gap-s1 rounded-md border border-line bg-surface px-s1 py-s1" role="group" aria-label={`Choices for ${name}`} data-testid="choices">
      {group("Your custom agents", own, "own")}
      {byProvider.map((p) => group(<><Mark provider={p} />{productName(p)}</>, shown.filter((c) => !c.custom && c.provider === p), p))}
      {onMake && (
        <button type="button" className="flex items-baseline gap-s2 rounded-md px-s1 py-0.5 text-left text-sm hover:bg-surface-2" onClick={onMake}>
          <span className="font-medium text-branch">+ Make a custom agent…</span>
          <span className="text-xs text-ink-3">A model plus your effort, instructions and rules</span>
        </button>
      )}
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
    <button type="button" className={`${pill} bg-sap-soft text-sap`} aria-expanded={open} data-testid="problems" onClick={onToggle}>
      {problemCount(c.problems.length)} {open ? "▴" : "▾"}
    </button>
  ) : null;

interface RowProps {
  kind: Kind;
  state: KindState;
  choiceOf(id: string): Choice;
  choices: Choice[];
  missing: string[];
  panel: Panel | null;
  adding: boolean;
  /* The agent whose problems are open under the row, if any. */
  probs: string | null;
  fresh: string | null;
  busy: boolean;
  builder: ReactNode;
  onPanel(p: Panel | null): void;
  onAdding(): void;
  onProbs(id: string): void;
  put(change: KindPut): void;
  onNavigate?: AgentsScreenProps["onNavigate"];
}

function Row({ kind, state, choiceOf: info, choices, missing, panel, adding, probs, fresh, busy, builder, onPanel, onAdding, onProbs, put, onNavigate }: RowProps) {
  const { name, blurb } = KIND_INFO[kind];
  const list = runOrder(state).map(info);
  const [first, ...rest] = list;
  const now = list.find((c) => c.ok);
  const learned = state.learned ? info(state.learned) : null;
  const own = [state.model, ...state.fallbacks].filter((x): x is string => !!x);
  // Each agent's "N problems" opens its own.
  const broken = list.find((c) => c.id === probs && c.problems.length > 0);
  const problems = (c: Choice) => <ProblemsButton c={c} open={probs === c.id} onToggle={() => onProbs(c.id)} />;
  // Its provider can be connected or signed in from Settings › Providers.
  const connectable = (c: Choice | undefined) => !!c?.provider && (c.blocker === "not_installed" || c.blocker === "signed_out");
  const toggle = (p: Panel) => onPanel(panel === p ? null : p);
  const [instr, setInstr] = useState(state.instructions ?? "");
  useEffect(() => setInstr(state.instructions ?? ""), [state.instructions]);

  let note: ReactNode = null;
  if (learned) {
    note = (
      <div className="flex flex-col gap-0.5 rounded-md bg-fawn-soft px-s1 py-0.5 text-sm" data-testid="learned">
        <span>
          brindle learned <b>{learned.label}</b> does better here ·{" "}
          <button type="button" className="text-fawn underline" disabled={busy} onClick={() => put({ locked: true })}>
            use mine instead
          </button>
        </span>
        <span className="text-xs text-ink-3">Lock this row to keep your choice.</span>
      </div>
    );
  } else if (first && !first.ok && now) {
    note = (
      <p className="flex flex-wrap items-baseline gap-x-1 rounded-md bg-fawn-soft px-s1 py-0.5 text-sm" data-testid="cant-run">
        {first.label} can't run: {first.why}. <b>{now.label}</b> runs it until then.
        {connectable(first) && <ConnectLink provider={first.provider} onNavigate={onNavigate} />}
      </p>
    );
  }
  if (!now) {
    note = (
      <p className="rounded-md bg-sap-soft px-s1 py-0.5 text-sm text-sap" data-testid="cant-run">
        Nothing here can run this now{first ? `: ${first.label} can't (${first.why})` : ""}. This work waits.
        {connectable(first) && <> <ConnectLink provider={first.provider} onNavigate={onNavigate} /></>}
      </p>
    );
  }

  const fallbacks = (
    <div className="flex flex-col gap-s1 rounded-md border border-line px-s2 py-s1" data-testid="fallback-list">
      <p className="text-xs text-ink-2">Tried in order when the one before it is at its limit or can't run. None left: the task waits and you're told.</p>
      <ol className="flex flex-col rounded-md bg-canvas-2 px-s1 py-0.5" aria-label={`${name} fallbacks`}>
        {rest.map((c, k) => {
          const i = own.indexOf(c.id);
          const mine = !!learned && i === 0;
          return (
            <li key={c.id} className="flex items-center gap-s1 py-0.5 text-sm" data-choice={c.id}>
              <span className="w-4 text-right text-xs text-ink-3">{k + 1}</span>
              <span className={`flex min-w-0 items-center gap-1 ${c.ok ? "" : "text-ink-3 line-through"}`}>
                <ChoiceName c={c} />
              </span>
              <CostTag cost={c.cost} />
              {!c.ok && <span className="text-xs text-ink-3">skipped: {c.why}</span>}
              {problems(c)}
              <span className="flex-1" />
              {mine ? (
                <span className={`${pill} bg-surface-2 text-ink-3`}>your pick</span>
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
        {rest.length === 0 && <li className="py-0.5 text-sm text-ink-3">No fallbacks yet.</li>}
        <li>
          <button type="button" className={`${quiet} -ml-s1`} aria-expanded={adding} onClick={onAdding}>
            {adding ? "−" : "+"} Add a fallback
          </button>
        </li>
      </ol>
      {adding && <Choices name={`${name} fallbacks`} choices={choices} skip={runOrder(state)} missing={missing} onPick={(id) => put({ fallbacks: [...own.slice(1), id] })} onNavigate={onNavigate} />}
    </div>
  );

  const more = (
    <div className="flex flex-col gap-s1 rounded-md border border-line px-s2 py-s1" data-testid="more">
      <div className="flex flex-wrap items-center gap-s2 text-sm">
        <span className="w-24 text-ink-3">Effort</span>
        <Seg name={`${name} effort`} options={EFFORT} value={(state.effort as (typeof EFFORT)[number]) ?? null} disabled={busy} onPick={(effort) => put({ effort })} />
      </div>
      <label className="flex flex-wrap items-start gap-s2 text-sm">
        <span className="w-24 text-ink-3">Instructions</span>
        <textarea
          className={`${field} min-h-16 min-w-0 flex-1`}
          value={instr}
          placeholder={`Added to whatever runs ${name} work.`}
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
      {learned && <p className="text-xs text-ink-3">Learning runs {learned.label} here now. Picking one locks this row, so learning won't change it.</p>}
      <Choices
        name={name}
        choices={choices}
        current={state.model}
        fresh={fresh}
        missing={missing}
        onPick={(id) => {
          onPanel(null);
          // What ran it before becomes its first fallback.
          put({ model: id, fallbacks: own.filter((x) => x !== id) });
        }}
        onMake={() => onPanel("make")}
        onNavigate={onNavigate}
      />
    </div>
  );

  const ext = panel === "pick" ? picking : panel === "fb" ? fallbacks : panel === "more" ? more : panel === "make" ? builder : null;
  const selected = panel === "pick" || panel === "fb" || panel === "make";
  return (
    <li className={`grid grid-cols-[12rem_minmax(0,1fr)] gap-x-s3 gap-y-s1 border-t border-line px-s2 py-s2 lg:grid-cols-[12rem_minmax(0,16rem)_minmax(0,1fr)_auto] ${selected ? "bg-surface-2" : ""}`} aria-label={`${name} work`} data-testid={`kind-${kind}`}>
      <div>
        <div className="text-sm font-semibold">{name}</div>
        <div className="text-xs text-ink-2">{blurb}</div>
      </div>
      <div className="flex min-w-0 flex-col items-start gap-1">
        {first ? (
          <button
            type="button"
            className={`inline-flex max-w-full items-center gap-1 rounded-md border border-line-strong bg-surface px-s1 py-0.5 text-sm hover:bg-surface-2 ${first.ok ? "" : "text-ink-3"}`}
            aria-label={`${name} runs on`}
            aria-expanded={panel === "pick"}
            data-choice={first.id}
            onClick={() => toggle("pick")}
          >
            <ChoiceName c={first} />
            <CostTag cost={first.cost} />
            {first.speed && <span className="text-ink-3">{first.speed}</span>}
            {learned && <span className={`${pill} bg-fawn-soft text-fawn`}>learned</span>}
            <span className="text-ink-3" aria-hidden>
              ▾
            </span>
          </button>
        ) : (
          <button type="button" className={button} aria-label={`${name} runs on`} aria-expanded={panel === "pick"} onClick={() => toggle("pick")}>
            Pick a model ▾
          </button>
        )}
        {first && problems(first)}
        {note}
      </div>
      <div className="text-sm">
        <button type="button" className="text-left" aria-expanded={panel === "fb"} data-testid="fallbacks" onClick={() => toggle("fb")}>
            <span className="text-ink-3">If it's at its limit: </span>
            <span data-testid="chain">
              {rest.length === 0
                ? "the task waits"
                : rest.map((c, i) => (
                    <span key={c.id}>
                      {i > 0 && ", then "}
                      <span className={c.ok ? "" : "text-ink-3 line-through"}>{c.label}</span>
                    </span>
                  ))}
            </span>{" "}
            <span className="text-branch">
              fallbacks ({rest.length}) {panel === "fb" ? "▴" : "▾"}
            </span>
        </button>
      </div>
      <div className="flex items-start gap-s1">
        <button
          type="button"
          className={`${pill} border ${state.locked ? "border-branch bg-branch-soft text-branch" : "border-line text-ink-2"}`}
          aria-pressed={state.locked}
          data-testid="lock"
          title={state.locked ? "Locked: learning won't change this row. Click to let it." : "Learning may run a better model here. Click to lock your choice."}
          disabled={busy}
          onClick={() => put({ locked: !state.locked })}
        >
          <span aria-hidden>{state.locked ? "🔒" : "🔓"}</span> {state.locked ? "Locked" : "Lock"}
        </button>
        <button type="button" className={`${quiet} text-xs`} aria-expanded={panel === "more"} onClick={() => toggle("more")}>
          More {panel === "more" ? "▴" : "▾"}
        </button>
      </div>
      {(ext || broken) && (
        <div className="col-span-full flex flex-col gap-s1 lg:col-start-2">
          {ext}
          {broken && <Problems c={broken} />}
        </div>
      )}
    </li>
  );
}

/* The guided path to a custom agent, under the row it was opened from:
   the five steps as one short form, each unlocking the next. */
function Builder({ kind, draft, setDraft, profiles, connected, rules, taken, choiceOf: info, current, busy, error, onCreate, onCancel, onNavigate }: {
  kind: Kind;
  draft: Draft;
  setDraft(d: Draft): void;
  profiles: RoutingProfileDTO[];
  connected: Set<string>;
  rules: RulePackInfo[];
  taken: string[];
  choiceOf(id: string): Choice;
  current: Record<Kind, KindState>;
  busy: boolean;
  error: string | null;
  onCreate(): void;
  onCancel(): void;
  onNavigate?: AgentsScreenProps["onNavigate"];
}) {
  const set = (patch: Partial<Draft>) => setDraft({ ...draft, ...patch });
  const providers = providersOf(profiles).filter((p) => p !== "native" || connected.has(p));
  const off = providers.filter((p) => !connected.has(p));
  const models = draft.provider ? modelsOf(draft.provider, profiles) : [];
  const picked = models.find((m) => m.model === draft.model);
  const problem = draftProblem(draft, taken);
  const name = cleanName(draft.name);
  const useFor = draft.useFor;
  const before = useFor ? current[useFor].model : null;
  const reach = !draft.name.trim() || !draft.purpose.trim() ? 0 : !draft.model ? 1 : 4;
  const step = (i: number, body: ReactNode) => (
    <div key={STEPS[i][0]} className={`grid gap-x-s3 gap-y-1 border-t border-line py-s1 md:grid-cols-[11rem_minmax(0,1fr)] ${i > reach ? "opacity-60" : ""}`} data-step={STEPS[i][0]}>
      <span className="flex items-baseline gap-1 text-sm font-semibold">
        <span className="inline-flex size-4 items-center justify-center rounded-full bg-branch-soft text-[10px] text-branch">{i + 1}</span>
        {STEPS[i][1]}
      </span>
      <div className="flex min-w-0 flex-col gap-s1">{body}</div>
    </div>
  );
  return (
    <section className={`${card} flex flex-col gap-s1 border-branch px-s2 py-s2`} aria-label="New custom agent" data-testid="builder">
      <header className="flex flex-col">
        <h3 className="text-sm font-semibold">New custom agent</h3>
        <p className="text-xs text-ink-2">A model plus how it should work. When it's made, it's in every model list, and {KIND_INFO[kind].name} can use it.</p>
      </header>
      {step(
        0,
        <>
          <label className="flex max-w-md flex-col gap-0.5 text-xs text-ink-3">
            Name
            <input className={`${field} ${mono}`} value={draft.name} placeholder="careful-dev" onChange={(e) => set({ name: e.target.value })} />
          </label>
          <label className="flex max-w-md flex-col gap-0.5 text-xs text-ink-3">
            What it's for
            <input className={field} value={draft.purpose} placeholder="Feature work, tests first, small commits" onChange={(e) => set({ purpose: e.target.value })} />
          </label>
        </>,
      )}
      {step(
        1,
        <>
          <div className="flex flex-wrap gap-1" role="group" aria-label="Provider">
            {providers.map((p) =>
              connected.has(p) ? (
                <button key={p} type="button" aria-pressed={draft.provider === p} className={`${pill} gap-1 border py-0.5 ${draft.provider === p ? "border-branch bg-branch-soft text-branch" : "border-line text-ink"}`} onClick={() => set({ provider: p, model: null })}>
                  <Mark provider={p} />
                  {productName(p)}
                  <span className="text-ink-3">{modelsOf(p, profiles).length} models</span>
                </button>
              ) : (
                <span key={p} className={`${pill} gap-1 border border-line py-0.5 text-ink-3`} data-provider-off={p}>
                  <Mark provider={p} />
                  {productName(p)} <span>not connected</span>
                </span>
              ),
            )}
          </div>
          {off.length > 0 && (
            <p className="text-xs text-ink-3">
              Only connected providers can run agents here. {off.map(productName).join(" and ")}: <ConnectLink provider={off[0]} onNavigate={onNavigate}>connect in Settings › Providers</ConnectLink>.
            </p>
          )}
          {draft.provider ? (
            <div className="grid gap-1 sm:grid-cols-2 xl:grid-cols-3" role="group" aria-label="Model">
              {models.map((m) => (
                <button key={m.name} type="button" aria-pressed={draft.model === m.model} className={`flex flex-col items-start rounded-md border px-s1 py-0.5 text-left ${draft.model === m.model ? "border-branch bg-branch-soft" : "border-line hover:bg-surface-2"}`} onClick={() => set({ model: m.model ?? null })}>
                  <span className="text-sm font-semibold">{prettyModel(m.model)}</span>
                  <span className="text-xs text-ink-3">
                    {COST_MARK[m.cost_tier]} {COST_WORDS[m.cost_tier]} · {speedOf(m)}
                  </span>
                  <span className="text-xs text-ink-2">{info(m.name).good}</span>
                </button>
              ))}
            </div>
          ) : (
            <p className="text-xs text-ink-3">Pick a provider to see its models.</p>
          )}
        </>,
      )}
      {step(
        2,
        <>
          <div className="flex flex-wrap items-center gap-s2 text-sm">
            <span className="w-24 text-ink-3">Effort</span>
            <Seg name="Effort" options={EFFORT} value={draft.effort as (typeof EFFORT)[number]} onPick={(effort) => set({ effort })} />
            <span className="text-xs text-ink-3">Higher thinks longer and costs more.</span>
          </div>
          <label className="flex flex-col gap-0.5 text-xs text-ink-3">
            Instructions
            <textarea className={`${field} min-h-16`} value={draft.instructions} placeholder="Write the failing test first. Keep each commit to one step." onChange={(e) => set({ instructions: e.target.value })} />
          </label>
          <div className="flex flex-col gap-0.5 text-sm" role="group" aria-label="Rules">
            <p className="text-xs text-ink-3">
              Rules are short instructions brindle adds to this agent's brief.{" "}
              <button type="button" className="text-branch underline" onClick={() => onNavigate?.("knowledge")}>
                See all rules
              </button>
            </p>
            {rules.map((r) => (
              <label key={r.name} className="grid cursor-pointer grid-cols-[1rem_minmax(0,10rem)_minmax(0,1fr)] items-baseline gap-x-s1" data-rule={r.name}>
                <input type="checkbox" className="accent-current" checked={draft.rules.includes(r.name)} onChange={() => set({ rules: draft.rules.includes(r.name) ? draft.rules.filter((x) => x !== r.name) : [...draft.rules, r.name] })} />
                <span className={mono}>{r.name}</span>
                <span className="text-ink-2">{r.description}</span>
              </label>
            ))}
            {rules.length === 0 && <p className="text-xs text-ink-3">No rule packs here yet.</p>}
          </div>
        </>,
      )}
      {step(
        3,
        <>
          <div className="flex flex-wrap items-center gap-s2 text-sm">
            <span className="text-ink-3">Use it for</span>
            <Seg<"any" | Kind>
              name="Use it for"
              options={["any", ...KINDS]}
              value={useFor ?? "any"}
              labelOf={(k) => (k === "any" ? "Any kind of work" : KIND_INFO[k].name)}
              onPick={(k) => set({ useFor: k === "any" ? null : k })}
            />
          </div>
          <p className="text-xs text-ink-3">
            {useFor
              ? `It runs ${KIND_INFO[useFor].name} work first${before ? `; ${info(before).label} becomes its first fallback` : ""}.`
              : "It can work at any level: it's saved and offered in every model list, so you can pick it for any kind of work, now or later."}
          </p>
        </>,
      )}
      {step(
        4,
        <>
          <p className="text-sm" data-testid="summary">
            {picked ? (
              <>
                <span className={mono}>
                  <b>{name || "…"}</b>
                </span>{" "}
                runs on {prettyModel(picked.model)} ({productName(picked.provider)}) <CostTag cost={picked.cost_tier} /> · {draft.effort} effort
                {draft.rules.length > 0 && <> · rules {draft.rules.join(", ")}</>}, and {useFor ? <>runs <b>{KIND_INFO[useFor].name}</b> work{before ? `, ahead of ${info(before).label}` : ""}</> : "can work at any level, picked from any model list"}.
              </>
            ) : (
              "Pick a model first."
            )}
          </p>
          <p className={`text-sm ${problem ? "text-sap" : "text-pass"}`} data-testid="check">
            {problem ?? "brindle checked it: the model is available and every rule pack is installed."}
          </p>
          {error && (
            <p role="alert" className={alert}>
              Could not create it: {error}
            </p>
          )}
          <div className="flex flex-wrap items-center gap-s1">
            <span className="text-xs text-ink-3">
              Saved to <span className={mono}>~/.brindle/agents/{name || "…"}.md</span>.
            </span>
            <span className="flex-1" />
            <button type="button" className={quiet} onClick={onCancel}>
              Cancel
            </button>
            <button type="button" className={primary} disabled={!!problem || busy} onClick={onCreate}>
              {busy ? "Creating…" : "Create agent"}
            </button>
          </div>
        </>,
      )}
    </section>
  );
}

export function AgentsScreen({ client, onNavigate }: AgentsScreenProps) {
  const { repo, setRepo, repos } = useRepo();
  const [routing, setRouting] = useState<RoutingDTO | null>(null);
  const [describe, setDescribe] = useState<Record<string, string>>({});
  const [rules, setRules] = useState<RulePackInfo[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<{ kind: Kind; panel: Panel } | null>(null);
  const [adding, setAdding] = useState<Kind | null>(null);
  /* The row and the agent whose problems are open there. */
  const [probs, setProbs] = useState<{ kind: Kind; id: string } | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [fresh, setFresh] = useState<string | null>(null);
  const seq = useRef(0);
  /* The repo on screen. A save or a load that ends after the picker moved to
     another repo is about that one: it must not read or show anything here
     (nor bump `seq`, which would drop this repo's own load). */
  const shown = useRef<string | null>(repo);
  shown.current = repo;

  const load = useCallback(() => {
    if (!repo || shown.current !== repo) return Promise.resolve();
    const mine = ++seq.current;
    return Promise.all([client.routing(repo), client.profiles(repo).catch(() => []), client.rulePacks(repo).catch(() => [])]).then(
      ([r, ps, rs]) => {
        if (mine !== seq.current || shown.current !== repo) return;
        setRouting(r);
        setDescribe(Object.fromEntries(ps.map((p) => [p.name, p.description])));
        setRules(rs);
        setLoadError(null);
      },
      (e: unknown) => {
        if (mine === seq.current) setLoadError(message(e));
      },
    );
  }, [client, repo]);

  useEffect(() => {
    setRouting(null);
    setOpen(null);
    setDraft(null);
    setBusy(false);
    setSaveError(null);
    void load();
  }, [load]);

  const profiles = useMemo(() => routing?.profiles ?? [], [routing]);
  const connected = useMemo(() => connectedProviders(profiles), [profiles]);
  const kinds = useMemo(() => (routing ? kindsOf(routing) : null), [routing]);
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
  const missing = providersOf(profiles).filter((p) => !connected.has(p) && p !== "native");
  const custom = profiles.filter(isCustom);

  const put = (kind: Kind, change: KindPut) => {
    if (!repo || frozen) return Promise.resolve();
    const target = repo;
    const here = () => shown.current === target;
    setBusy(true);
    setSaveError(null);
    return client
      .putRouting({ repo: target, scope: "local", kinds: { [kind]: change } })
      .then(() => (here() ? load() : undefined))
      .catch((e: unknown) => here() && setSaveError(message(e)))
      .finally(() => here() && setBusy(false));
  };

  const panelFor = (k: Kind): Panel | null => (open?.kind === k ? open.panel : null);
  const setPanel = (k: Kind, p: Panel | null) => {
    setOpen(p ? { kind: k, panel: p } : null);
    setAdding(null);
    if (p === "make") {
      setDraft(newDraft(k));
      setCreateError(null);
    }
  };

  const create = async (k: Kind) => {
    if (!repo || !draft || !kinds || !draft.provider || !draft.model) return;
    const name = cleanName(draft.name);
    const target = repo;
    const here = () => shown.current === target;
    setBusy(true);
    setCreateError(null);
    try {
      await client.createProfile({ repo, name, description: draft.purpose.trim(), provider: draft.provider, model: draft.model, effort: draft.effort, instructions: draft.instructions, rules: draft.rules });
      // It runs the kind chosen; what ran it before becomes its first fallback.
      if (draft.useFor) {
        const was = kinds[draft.useFor];
        const own = [was.model, ...was.fallbacks].filter((x): x is string => !!x);
        await client.putRouting({ repo, scope: "local", kinds: { [draft.useFor]: { model: name, fallbacks: own } } });
      }
      // The picker moved to another repo meanwhile: nothing here to show.
      if (!here()) return;
      setFresh(name);
      setDraft(null);
      await load();
      // The builder closes; the row it went to shows its list, the new agent in it.
      if (here()) setOpen({ kind: draft.useFor ?? k, panel: "pick" });
    } catch (e) {
      if (here()) setCreateError(message(e));
    } finally {
      if (here()) setBusy(false);
    }
  };

  const row = (k: Kind) => {
    if (!kinds) return null;
    return (
      <Row
        key={k}
        kind={k}
        state={kinds[k]}
        choiceOf={info}
        choices={offeredFor(k, profiles).map((p) => choiceOf(p, connected, describe))}
        missing={missing}
        panel={panelFor(k)}
        adding={adding === k}
        probs={probs?.kind === k ? probs.id : null}
        fresh={fresh}
        busy={busy || frozen}
        builder={
          draft && (
            <Builder
              kind={k}
              draft={draft}
              setDraft={setDraft}
              profiles={profiles}
              connected={connected}
              rules={rules}
              taken={profiles.map((p) => p.name)}
              choiceOf={info}
              current={kinds}
              busy={busy}
              error={createError}
              onCreate={() => void create(k)}
              onCancel={() => setPanel(k, null)}
              onNavigate={onNavigate}
            />
          )
        }
        onPanel={(p) => setPanel(k, p)}
        onAdding={() => setAdding(adding === k ? null : k)}
        onProbs={(id) => setProbs(probs?.kind === k && probs.id === id ? null : { kind: k, id })}
        put={(change) => void put(k, change)}
        onNavigate={onNavigate}
      />
    );
  };

  return (
    <section className="flex flex-col gap-s2" aria-label="Agents">
      <PageHeader title="Agents" lede="Which model runs each kind of work, and what takes over at a limit.">
        {repos && repos.length > 1 && (
          <span role="group" aria-label="Repo" className="flex flex-wrap gap-1">
            {repos.map((r) => (
              <button key={r.repo_root} type="button" aria-pressed={r.repo_root === repo} className={`${pill} border py-0.5 ${mono} ${r.repo_root === repo ? "border-branch bg-branch-soft text-branch" : "border-line text-ink-2"}`} onClick={() => setRepo(r.repo_root)}>
                {r.name}
              </button>
            ))}
          </span>
        )}
      </PageHeader>

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

      <section className={`${card} flex flex-col gap-s2 px-s2 py-s2`} aria-labelledby="how-title">
        <header className="flex flex-wrap items-baseline gap-x-s2">
          <h2 id="how-title" className={h2}>
            How a task gets run
          </h2>
          <span className="text-sm text-ink-2">Automatic. You only choose what runs each kind of work, below.</span>
        </header>
        <ol className="grid gap-s1 md:grid-cols-2 xl:grid-cols-4" aria-label="How a task is run">
          {FLOW.map(([t, b], i) => (
            <li key={t} className="flex gap-s1 rounded-md bg-canvas-2 px-s1 py-s1">
              <span className="inline-flex size-5 shrink-0 items-center justify-center rounded-full bg-branch-soft text-xs text-branch">{i + 1}</span>
              <span className="flex flex-col">
                <b className="text-sm">{t}</b>
                <span className="text-xs text-ink-2">{b}</span>
              </span>
            </li>
          ))}
        </ol>
      </section>

      {kinds && (
        <section className={`${card} flex flex-col py-s2`} aria-labelledby="work-title">
          <header className="flex flex-wrap items-baseline gap-x-s2 px-s2 pb-s1">
            <h2 id="work-title" className={h2}>
              Who does each kind of work
            </h2>
            <span className="text-sm text-ink-2">
              Pick a model, or one of your custom agents. Only connected providers' models are offered. brindle's hosted learning may run a better model for a kind of work, and says so on the row; lock a row to keep your choice.
            </span>
          </header>
          {policy?.applies && (
            <p className="px-s2 pb-s1 text-sm text-fawn" data-testid="policy">
              🔒 Your org's policy applies: models it doesn't allow aren't offered.
              {frozen && ` Nothing here can change now: ${policy.reason ?? "the policy couldn't be read"}.`}
            </p>
          )}
          {saveError && (
            <p role="alert" className={`${alert} mx-s2 mb-s1`}>
              Could not save: {saveError}
            </p>
          )}
          <div className={`${label} px-s2 py-s1`}>Sizes · the supervisor gives every task one</div>
          <ul className="flex flex-col">{SIZES.map(row)}</ul>
          <div className={`${label} border-t border-line px-s2 py-s1`}>Every branch, and the session itself</div>
          <ul className="flex flex-col">{(["reviewer", "supervisor"] as const).map(row)}</ul>
          {missing
            .filter((p) => KINDS.some((k) => runOrder(kinds[k]).some((id) => byName.get(id)?.provider === p)))
            .map((p) => (
              <p key={p} className="flex flex-wrap items-center gap-1 border-t border-line px-s2 pt-s1 text-sm text-ink-2" data-missing={p}>
                <Mark provider={p} />
                {productName(p)} isn't connected, so its models aren't offered and rows skip it.
                <ConnectLink provider={p} onNavigate={onNavigate} />
              </p>
            ))}
          <p className="border-t border-line px-s2 pt-s1 text-xs text-ink-3" data-testid="custom-agents">
            {custom.length > 0 ? (
              <>
                Your custom agents:{" "}
                {custom.map((p, i) => (
                  <span key={p.name}>
                    {i > 0 && ", "}
                    <span className={mono}>{p.name}</span>
                    {(p.lint_errors?.length ?? 0) > 0 && <span className="text-sap"> ({problemCount(p.lint_errors!.length)})</span>}
                  </span>
                ))}
                . They're listed with the models in every picker; "Make a custom agent…" is at the foot of it.
              </>
            ) : (
              <>No custom agents yet: "Make a custom agent…" is at the foot of every row's model list.</>
            )}
          </p>
        </section>
      )}
    </section>
  );
}
