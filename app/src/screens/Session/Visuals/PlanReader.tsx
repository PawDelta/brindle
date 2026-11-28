/* A design doc (brindle/plans.py, brindle/plan_docs.py), opened over the whole window like any
   visual. Unlike a visual it isn't the agent's HTML: brindle renders it itself (DocText.tsx:
   elements only, no raw HTML, no fetched images), so it needs no frame, and every control on it
   is the app's own. One reader for every doc, wherever it was opened from.

   Top to bottom: the toolbar (DocBar.tsx: Back, the title with its one number, who else is in the
   doc, the Markdown switch, the mode, Share and the doc's actions); for a shared doc, one strip
   that says whether its text is live or locked for sign-off (DocFreeze.tsx); under it, whenever
   the person can type, the formatting toolbar (DocFormat.tsx). Then the page: the title and its
   status in one line (DocStatus.tsx), the overview, each section with its number in the margin,
   then each decision as a table of its alternatives with who voted for which. Beside it, the side
   column (DocReview.tsx): Comments, Decisions (and the sign-off sheet), History, Tasks. Along the
   bottom, a bar that says the next step in words, with its actions (Jump to it, Request changes,
   Confirm a decision, then Sign off), and under it the person's note with its attachments.

   Three modes, in the toolbar. Viewing reads. Suggesting: highlighting text and Suggest edit
   writes the change on the page as a tracked change (struck, and inserted in green), sent with a
   reason from its card. Editing: a section's own Edit control opens its text where it reads: on
   a doc in team review, the live text (DocEditor.tsx), shared with everyone writing in it as they
   type; on a personal doc, its Markdown in a box saved as a new revision. The Markdown switch
   shows the doc as its source in any mode (DocMarkdown.tsx). In any mode, highlighting text
   offers Comment, Suggest edit and Ask; what the person writes lands in the Comments tab, tied to
   that text by its number.

   The next decision to take is led by the bottom bar, and marked in the doc by a green divider.
   Sign off starts the session's queued workers, and the split's task cards stay on top as a
   live board; Request changes sends the person's picks, threads and note back to the supervisor,
   who publishes a new revision. A write names the revision on screen: a newer one is a 409. */

import { Suspense, lazy, useCallback, useContext, useEffect, useMemo, useRef, useState, useSyncExternalStore, type MouseEvent, type ReactNode, type TextareaHTMLAttributes } from "react";
import { LoroDoc } from "loro-crdt";
import { DaemonError, fromBase64, type DaemonClient, type DocRevisionComparison, type DocScope, type LibraryPerson, type LiveDocState, type Plan, type PlanAnchor, type PlanChange, type SharedDesign, type VisualDTO } from "../../../api/client";
import { liveReviewGate } from "../../../api/liveReview";
import type { PlanDecisionDTO, PlanSectionDTO, PlanThreadDTO } from "../../../api/schema";
import { DocReference } from "../../../components/DocReference";
import { LoadingButton } from "../../../components/LoadingButton";
import { ago } from "../../../components/ProviderConnect";
import { PaperclipIcon } from "../../../components/icons";
import { button, danger, field, label, main, pill, quiet } from "../../../components/ui";
import { CloudLoginStatus, useCloudLogin } from "../../Inbox/CloudSignIn";
import type { ExportClient } from "../../Knowledge/ExportMenu";
import type { PlanDocClient, PlanShareLinkClient, PlanSharingClient, PlansClient, VisualsClient } from "./client";
import { BackButton, KeepButton, fileAttachment, fileName, message, useEscBack } from "./chrome";
import { ReaderBar } from "./DocBar";
import { Diagram } from "./DocDiagram";
import { FormatBar, FormatProvider, textareaTarget, useFormatTarget } from "./DocFormat";
import { DocumentHistory, DocHistoryComparison, type HistoryEvent, type WorkItem } from "./DocHistory";
import { DocMarkdown } from "./DocMarkdown";
import { DraftCard, Review, ReviewStrip, SignOffSheet, isSupervisorReplying, type Draft, type ReviewTab, type SignOffRow, type ThreadActions, type TodoItem } from "./DocReview";
import { SharePanel } from "./SharePanel";
import { Policies } from "./DocPolicies";
import type { LiveClient } from "./DocEditor";
import { DocPresence, type LiveCapability } from "./DocPresence";
import { DocStatus } from "./DocStatus";
import { DocText, changeBetween, selectedRange, toPoints, toUnits, type Mark } from "./DocText";
import { ExportButton } from "./ExportButton";
import { ShareLinkSection, planLinkTarget, useShareLink, type ShareLinkTarget } from "./ShareLink";
import { Alongside, Decision, Tasks, displayName, overlaps, passChip, personName, shortName, useAccount } from "./PlanCards";
import { Annotated, DecisionCard, type OwnAnswers, MarginNote, Menu, ModeMenu, Outline, SectionText, Tick, decisionsInOrder, docLayout, menuNote, menuRow, menuTitle, onSpine, placeDecisions, spineParts } from "./PlanDesignDoc";
import type { Work } from "./WorkPanel";
import { planSource, type DocSource } from "./docSource";
import { LibraryNetworkNote, SlowConnectionNote, libraryNetwork, useSlowPending, type LibraryNetwork } from "./LibraryNetwork";
import { StoreContext } from "../../../store/useStore";
import { PlanDraftScope, readPlanDraft, savePlanDraft, usePlanDraft, usePlanDraftScope } from "./planDrafts";

type Client = VisualsClient & PlansClient & PlanDocClient & Partial<ExportClient> & Partial<Pick<DaemonClient, "attach" | "cancelPlan" | "stopAlongside">> & Partial<PlanSharingClient> & Partial<LiveClient> & Partial<PlanShareLinkClient>;

/* How far along a plan is: its revision, then decided or not. Revisions only grow and a decided
   revision never reopens, so a response with less progress than the one on screen is stale. */
/* The live editor brings CodeMirror and Loro's WebAssembly: loaded when a section is first edited live. */
const DocEditor = lazy(() => import("./DocEditor").then((m) => ({ default: m.DocEditor })));
const SuggestEditor = lazy(() => import("./DocEditor").then((m) => ({ default: m.SuggestEditor })));

const progress = (p: Plan) => p.rev * 2 + (p.state === "open" ? 0 : 1);
const noSubscribe = () => () => {};

/* The refusals that only mean the plan moved on under the screen (a newer revision or version, or
   a verdict given elsewhere): the doc is read again. Any other refusal (a template's empty required
   section, a closed gate, a lock) says why in the engine's or the Library's own words. */
const MOVED = new Set(["stale", "decided", "conflict", "withdrawn"]);

function refusal(e: unknown): string {
  if (!(e instanceof DaemonError)) return `Couldn't send: ${message(e)}`;
  if (e.code === "gate_unverified") return message(e);
  if (e.status === 409 && MOVED.has(e.code)) return "This plan changed since you opened it (a new revision, or it was already decided). It is being read again.";
  // The daemon's own 403s (a session locked to another identity) aren't the Library's.
  if (e.status === 403 && e.code !== "locked") return `The Library refused this for your account: ${message(e)}`;
  return `Couldn't send: ${message(e)}`;
}

/* The doc's writes, kept in order by the reader: `begin` is refused while another is out, and
   `end` hands back the engine's reply (the plan as it is now), or null when the write failed. */
export interface Writes {
  begin(): boolean;
  /* `readBack`: told each time a read that follows this write settles: false for a failed one (it
     stays waiting), then true once one succeeds (the doc on screen is the Library's as of after
     the write), after which it is told nothing more. */
  end(plan: Plan | null, readBack?: (ok: boolean) => void): void;
  /* Read the doc again now (a read-back that failed, tried again). */
  readAgain?(): void;
}

export type Mode = "viewing" | "suggesting" | "editing";
export const MODES: { id: Mode; name: string; hint: string }[] = [
  { id: "viewing", name: "Viewing", hint: "Read, comment and decide" },
  { id: "suggesting", name: "Suggesting", hint: "Your edits become suggestions to accept" },
  { id: "editing", name: "Editing", hint: "Your edits change the text, as a new revision" },
];

/* Whether the review column is collapsed: this viewer's choice, kept on this device. */
export const REVIEW_COLLAPSED_KEY = "brindle.plan.reviewCollapsed";
function storedCollapsed(): boolean {
  try {
    return localStorage.getItem(REVIEW_COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

/* The revision this viewer last read of each doc, kept on this device: the sign-off sheet says what
   changed since. Reading a doc moves it on to the revision on screen. */
export const LAST_READ_KEY = "brindle.plan.lastRead.";
function useLastRead(id: string, rev: number): number | null {
  const [before] = useState(() => {
    try {
      const got = Number(localStorage.getItem(`${LAST_READ_KEY}${id}`));
      return Number.isFinite(got) && got > 0 ? got : null;
    } catch {
      return null;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(`${LAST_READ_KEY}${id}`, String(rev));
    } catch {
      // Not kept: the sheet only says less next time.
    }
  }, [id, rev]);
  return before;
}

/* A box of Markdown the formatting toolbar writes into while it has the focus. */
function FormatTextarea({ value, onValue, ...rest }: Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "value" | "onChange"> & { value: string; onValue(value: string): void }) {
  const box = useRef<HTMLTextAreaElement>(null);
  const target = useFormatTarget({ apply: (c) => textareaTarget(box.current, onValue).apply(c) });
  return <textarea ref={box} {...rest} value={value} onChange={(e) => onValue(e.target.value)} onFocus={target.focused} />;
}

/* What the reader's toolbar is given by whoever opened the doc. */
export interface ReaderChrome {
  /* Back and the title. */
  lead: ReactNode;
  badges?: ReactNode;
  actions?: ReactNode;
  modes: typeof MODES;
  allowed(mode: Mode): boolean;
  onMode(mode: Mode): void;
  /* Back to where the doc was opened from (the planning doc's "Back to the session" once approved). */
  onBack?: () => void;
}

/* The doc from its source, kept current, with its writes kept in order. `version` changing asks
   for one more read. */
/* `failed`: told of a failed read that counts (the latest read, while this reader is still open);
   never of one a newer read or a closed reader has made obsolete. */
export function useDoc<P extends Plan>(source: DocSource<P>, version: unknown, failed?: (e: unknown) => void, observeReads?: (read: () => void) => () => void) {
  const [doc, setDoc] = useState<P | null>(null);
  const onFailed = useRef(failed);
  onFailed.current = failed;
  const open = useRef(true);
  useEffect(() => {
    open.current = true;
    return () => { open.current = false; };
  }, []);
  const [error, setError] = useState<string | null>(null);
  // Every write to the doc is followed by one more read of it.
  const [again, setAgain] = useState(0);
  const seq = useRef(0);
  const selected = useRef(source.id);
  selected.current = source.id;
  // Reads and replies to writes can land in any order: the plan on screen only ever moves forward
  // (a newer revision, or this revision decided), and only for the plan still open here. With
  // `newer`, a plan no further along than the one on screen doesn't replace it.
  const latest = useRef<P | null>(null);
  const place = useCallback((p: P, newer = false) => {
    if (p.id !== selected.current) return false;
    const now = latest.current?.id === p.id ? latest.current : null;
    if (now && (progress(now) > progress(p) || (newer && progress(now) === progress(p)))) return false;
    // Votes, permissions and sign-offs can change without a revision or thread change.
    if (now && now.rev === p.rev && (now === p || JSON.stringify(now) === JSON.stringify(p))) return true;
    latest.current = p;
    setDoc(p);
    return true;
  }, []);
  // Comments, picks and replies change the plan without moving it along, so nothing in a snapshot
  // says which of two is the later. Order is kept by how they are asked for instead. Only the
  // latest read counts. Writes go one at a time (`out`), so two replies can't cross. A read shown
  // while a write is out (`passed`) may already be past it, so that write's reply then only
  // counts if it is further along. And when a write comes back, every read still out is
  // dropped (it may predate the write) and the plan is read once more.
  const write = useRef({ out: false, passed: false, access: 0 });
  // Counts the times access to the doc was lost: the reply to a write begun before that must not
  // bring the doc back. Only a read made since can.
  const access = useRef(0);
  const writes = useMemo<Writes>(() => ({
    begin: () => {
      if (write.current.out) return false;
      write.current = { out: true, passed: false, access: access.current };
      return true;
    },
    end: (p, readBack) => {
      if (p && write.current.access === access.current) place(p as P, write.current.passed);
      write.current = { out: false, passed: false, access: access.current };
      seq.current++;
      readBacks.current.push(...(readBack ? [readBack] : []));
      setAgain((n) => n + 1);
    },
    readAgain: () => setAgain((n) => n + 1),
  }), [place]);
  // Told when a read after a write settles (the latest read, begun after that write ended). A
  // failed one leaves them waiting for the next read that succeeds.
  const readBacks = useRef<((ok: boolean) => void)[]>([]);
  const settle = (ok: boolean) => {
    const waiting = readBacks.current;
    if (ok) readBacks.current = [];
    for (const tell of waiting) tell(ok);
  };
  useEffect(() => {
    let active = true;
    const read = () => {
      const mine = ++seq.current;
      void source.read().then(
        (p) => {
          if (!active || mine !== seq.current) return;
          if (place(p) && write.current.out) write.current.passed = true;
          setError(null);
          settle(true);
        },
        (e) => {
          if (!active || mine !== seq.current) return;
          settle(false);
          setError(message(e));
          if (source.lost?.(e)) {
            access.current++;
            latest.current = null;
            setDoc(null);
          }
          if (open.current) onFailed.current?.(e);
        },
      );
    };
    read();
    const stop = observeReads?.(read);
    return () => { active = false; stop?.(); };
  }, [source, place, version, again, observeReads]);
  return { doc: doc && doc.id === source.id ? doc : null, error, writes };
}

/* The toolbar's Viewing / Suggesting / Editing control. */
export function ModePicker({ modes, current, allowed, onPick }: { modes: typeof MODES; current: Mode; allowed(mode: Mode): boolean; onPick(mode: Mode): void }) {
  return (
    <div role="radiogroup" aria-label="Mode" className="inline-flex overflow-hidden rounded-md border border-line-strong text-xs">
      {modes.map((m) => (
        <button key={m.id} type="button" role="radio" aria-checked={current === m.id} disabled={!allowed(m.id)} title={m.hint} onClick={() => onPick(m.id)}
          className={`cursor-pointer px-2.5 py-0.5 focus-visible:-outline-offset-2 disabled:cursor-not-allowed disabled:opacity-45 ${current === m.id ? "bg-branch-soft font-medium text-branch" : "text-ink-3 enabled:hover:text-ink"}`}>
          {m.name}
        </button>
      ))}
    </div>
  );
}

export function PlanReader({ client, visual, repo, onBack, asking, onChanged, backLabel, work }: {
  client: Client;
  /* The design's work items, when it was opened from the Library. */
  work?: Work;
  visual: VisualDTO;
  repo?: string;
  onBack(): void;
  asking?: boolean;
  /* Where Back goes, in words ("Library"). */
  backLabel?: string;
  /* The engine's row after a Keep, ahead of the bus. */
  onChanged(visual: VisualDTO): void;
}) {
  useEscBack(onBack);
  const [exporting, setExporting] = useState(false);
  const [mode, setMode] = useState<Mode>("viewing");
  const store = useContext(StoreContext);
  const currentPlan = useRef<Plan | null>(null);
  const planUpdates = useMemo(() => {
    let cursor = 0;
    let changed = 0;
    return () => {
      const state = store?.getState();
      const events = state?.recent ?? [];
      const plan = currentPlan.current?.id === visual.id ? currentPlan.current : null;
      for (let i = events.length - 1; i >= 0 && events[i].id > cursor; i--) {
        const event = events[i];
        const inSession = event.root_id === visual.session || event.payload.caller_id === visual.session;
        const changedDoc = inSession
          && (event.kind === "visual.plan_changed" || event.kind === "visual.answered" || event.kind === "visual.published")
          && (event.payload.visual == null || event.payload.visual === visual.id);
        // The plan also contains the state of its split and work alongside it.
        const changedTask = event.task_id != null
          && (event.kind.startsWith("task.") || event.kind === "agent.status" || event.kind === "agent.pipeline" || event.kind === "agent.result" || event.kind === "agent.closed")
          && (plan?.split.some((task) => task.task_id === event.task_id) || plan?.alongside?.some((task) => task.task_id === event.task_id));
        const changedWorker = event.agent_id != null
          && (event.kind === "agent.pipeline" || event.kind === "agent.result" || event.kind === "agent.closed")
          && plan?.split.some((task) => task.agent_id === event.agent_id);
        const newWork = inSession && (event.kind === "task.queued" || event.kind === "task.started");
        const changedChecks = inSession && event.kind === "checks.changed" && !!plan?.split.length;
        if (changedDoc || changedTask || changedWorker || newWork || changedChecks) {
          changed = event.id;
          break;
        }
      }
      // Retain the last invalidation even after its event leaves the bounded list.
      cursor = Math.max(cursor, events.at(-1)?.id ?? 0);
      return `${state?.eventLossGeneration ?? ""}:${changed}`;
    };
  }, [store, visual.session, visual.id]);
  const updates = useSyncExternalStore(store?.subscribe ?? noSubscribe, planUpdates);
  // Read again whenever the session's row says the plan changed (a revision, a verdict).
  const changed = `${visual.id}:${visual.rev}:${visual.answer?.at ?? ""}`;
  const source = useMemo(() => planSource(client, visual.id), [client, visual.id]);
  // A Stop on work running alongside the plan reads the plan again, ahead of the bus.
  const [stops, setStops] = useState(0);
  const { doc: shown, error, writes } = useDoc(source, `${changed}|${updates}|${stops}`);
  currentPlan.current = shown;
  const stopAlongside = client.stopAlongside && (async (task: string) => {
    await client.stopAlongside!(visual.id, task);
    setStops((n) => n + 1);
  });
  const exportClient = client.exportVisual && client.exportDoc && client.docPolicy && client.docExportConfig ? (client as ExportClient) : undefined;
  // A decided plan is read, not edited; the engine takes a suggestion until it is approved.
  const allowed = (m: Mode) => m === "viewing" || (!!shown && (m === "editing" ? shown.state === "open" : shown.state !== "approved"));
  const current = allowed(mode) ? mode : "viewing";
  const lead = (
    <>
      <BackButton onBack={onBack} asking={asking} label={backLabel} />
      <span className="mx-1 h-5 w-px bg-line" aria-hidden />
      <h2 title={visual.title} className="min-w-0 truncate text-[13px] font-semibold">{visual.title}</h2>
    </>
  );
  const badges = shown?.state === "approved" && (
    <span className={`${passChip} font-mono`} data-testid="plan-board-progress">
      approved · {shown.split.filter((w) => w.test_state === "passing").length} of {shown.split.length} green
    </span>
  );
  const actions = (
    <>
      <KeepButton client={client} visual={visual} onKept={onChanged} />
      {exportClient && shown && (
        <ExportButton key={`${shown.id}:${shown.rev}`} client={exportClient} repo={repo} open={exporting} onOpenChange={setExporting} source={{ kind: "plan", id: shown.id, rev: shown.rev, markdown: shown.markdown }} />
      )}
    </>
  );
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col" data-testid="plan-reader">
      {shown ? (
        <DesignDoc key={shown.id} source={source} author={{ client, visual }} plan={shown} mode={current} writes={writes} work={work}
          shareLink={planLinkTarget(client, shown.id, repo)}
          chrome={{ lead, badges, actions, modes: MODES, allowed, onMode: setMode, onBack }}
          top={shown.state === "approved" ? (
            <section aria-label="Plan board" className="flex max-h-[50vh] shrink-0 flex-col gap-3.5 overflow-auto border-b border-line px-s3 py-5">
              <Tasks plan={shown} shared={overlaps(shown)} />
            </section>
          ) : !!shown.alongside?.length && (
            <div className="max-h-[30vh] shrink-0 overflow-auto border-b border-line px-s3 py-3">
              <Alongside rows={shown.alongside} onStop={stopAlongside || undefined} />
            </div>
          )} />
      ) : (
        <>
          <ReaderBar lead={lead} version={<span className="whitespace-nowrap text-xs text-ink-3" data-testid="plan-rev">{`rev ${visual.rev}`}</span>} actions={actions} />
          <p className="p-s3 text-sm text-ink-3">{error ?? "Loading…"}</p>
        </>
      )}
    </div>
  );
}

const LEDE = "lede";
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const heading = "relative m-0 flex items-baseline justify-between gap-3 font-serif text-[17px] font-semibold leading-[1.3] text-ink";
const margin = "absolute -left-7 top-[0.3em] font-mono text-xs font-normal text-ink-3";
const aside = "inline-flex items-center gap-1.5 whitespace-nowrap font-sans text-xs font-normal text-ink-3";
const show = (el: Element | null | undefined, block: ScrollLogicalPosition = "start") => el?.scrollIntoView?.({ block, behavior: "smooth" });

/* What a highlight offers, where it was made: a range of a section's text, or a diagram. */
type Picked = { x: number; y: number } & ({ kind: "text"; section: string; start: number; quote: string } | { kind: "diagram"; section: string });

function VerdictNote({ hasFeedback, children }: {
  hasFeedback: boolean;
  children(note: string, setNote: (note: string) => void, canRequest: boolean): ReactNode;
}) {
  const [note, setNote] = usePlanDraft<string>("verdict");
  return children(note ?? "", (next) => setNote(next || null), hasFeedback || !!note?.trim());
}

/* brindle's own sign-in, for a plan the Library can't confirm until it is renewed. */
function LibrarySignIn() {
  const login = useCloudLogin();
  return (
    <span className="grid justify-items-start gap-1">
      <button type="button" className={`${quiet} -my-0.5 text-xs`} disabled={login.busy} onClick={login.start}>Sign in</button>
      <CloudLoginStatus job={login.job} compact />
    </span>
  );
}

/* Someone reviewing a design another person shared: what the Library lets them do, and who wrote it.
   No plan here, so nothing to approve or build. */
export interface Teammate {
  me?: string;
  rights: { comment: boolean; suggest: boolean; signOff: boolean };
  signOff?: SharedDesign["sign_off"];
  signOffs?: SharedDesign["sign_offs"];
  signedOff: boolean;
  author: string | null;
  directory: LibraryPerson[];
}

export function DesignDoc({ source, author, teammate: receivedTeammate, plan: receivedPlan, mode: chosenMode, writes, liveClient, chrome, top, history = null, workItems = null, workItem = null, work, shareLink }: {
  source: DocSource;
  /* The design in the Library, for someone who may edit it: the Share menu's Copy link and Revoke link. */
  shareLink?: ShareLinkTarget;
  /* The design's work items in the tracker, from the Library: the Tasks tab, the status line, "Create work item". */
  work?: Work;
  /* The doc's author, on this machine: sharing, the verdict and the build. */
  author?: { client: Client; visual: VisualDTO };
  teammate?: Teammate;
  plan: Plan;
  mode: Mode;
  writes: Writes;
  liveClient?: LiveClient;
  /* The toolbar's parts from whoever opened the doc. */
  chrome?: ReaderChrome;
  /* Shown between the toolbar and the page (the approved plan's live board). */
  top?: ReactNode;
  /* The History tab's timeline, and the tracker's items under Tasks: null until they are known. */
  history?: HistoryEvent[] | null;
  workItems?: WorkItem[] | null;
  /* The design's own linked work item, in its status line. */
  workItem?: WorkItem | null;
}) {
  const scope = receivedTeammate && !receivedTeammate.me ? null : JSON.stringify([receivedPlan.id, receivedTeammate?.me ?? null]);
  return (
    <PlanDraftScope.Provider key={scope} value={scope}>
      <FormatProvider>
        <DesignDocBody source={source} author={author} teammate={receivedTeammate} plan={receivedPlan} mode={chosenMode} writes={writes} liveClient={liveClient} chrome={chrome} top={top} history={history} workItems={workItems} workItem={workItem} work={work} shareLink={shareLink} />
      </FormatProvider>
    </PlanDraftScope.Provider>
  );
}

function DesignDocBody({ source, author, teammate: receivedTeammate, plan: receivedPlan, mode: chosenMode, writes, liveClient, chrome, top, history, workItems, workItem: receivedWorkItem, work, shareLink }: {
  work?: Work;
  shareLink?: ShareLinkTarget;
  source: DocSource;
  author?: { client: Client; visual: VisualDTO };
  teammate?: Teammate;
  plan: Plan;
  mode: Mode;
  writes: Writes;
  liveClient?: LiveClient;
  chrome?: ReaderChrome;
  top?: ReactNode;
  history: HistoryEvent[] | null;
  workItems: WorkItem[] | null;
  workItem: WorkItem | null;
}) {
  const client = author?.client;
  // The design's own item sits in the status line with its state as last read.
  const epic = work?.state?.epic;
  const workItem: WorkItem | null = epic ? { key: epic.display, title: epic.display, state: epic.state ?? "linked", done: epic.done, active: !!epic.state && !epic.done, url: epic.url ?? undefined } : receivedWorkItem;
  const docLive =liveClient ?? (receivedPlan.gate && client?.visualLive ? (client as LiveClient) : null);
  const [liveState, setLiveState] = useState<LiveDocState | null>(null);
  const [liveCapability, setLiveCapability] = useState<LiveCapability>("unknown");
  const useLiveReview = !!docLive && liveCapability !== "unsupported";
  // A version frozen for sign-off can't change in any view: the reader only reads (and comments).
  const locked = useLiveReview && liveState?.lock === "sign_off";
  const mode: Mode = locked ? "viewing" : chosenMode;
  const frozen = useLiveReview && liveState?.lock === "sign_off" ? liveState.version : null;
  // A frozen version reads as its own text (its snapshot), which may have moved on from the
  // revision's: what is signed is what is shown, on the page and in the Markdown view.
  const frozenTexts = useMemo(() => {
    if (!frozen?.snapshot) return null;
    try {
      const snapshot = new LoroDoc();
      snapshot.import(fromBase64(frozen.snapshot));
      return new Map(Object.entries(snapshot.toJSON() as Record<string, unknown>).filter((e): e is [string, string] => typeof e[1] === "string"));
    } catch {
      return null;
    }
  }, [frozen?.snapshot]);
  // While the text is live it reads as everyone has typed it (DocPresence keeps it), not as the
  // revision: Done editing, and a Reopen, show the live text, never the older revision's.
  const [liveTexts, setLiveTexts] = useState<Map<string, string> | null>(null);
  const shownTexts = frozenTexts ?? (useLiveReview && !locked ? liveTexts : null);
  // Counts this person's changes to the review while a version is frozen: each asks the live
  // channel for the version again, so its sign-offs are the Library's now.
  const [refreshes, setRefreshes] = useState(0);
  const [raw, setRaw] = useState(false);
  const [sharingOpen, setSharingOpen] = useState(false);
  // Asked again as the menu opens, once the plan is shared (sharing is what puts it in the Library),
  // and after Stop sharing for everyone, which turns the link off.
  const [unshared, setUnshared] = useState(0);
  // Stop sharing and build stopped sharing but built nothing: the personal doc reads differently.
  const [personalDiffers, setPersonalDiffers] = useState(false);
  const link = useShareLink(shareLink, `${sharingOpen}:${!!receivedPlan.gate}:${unshared}`);
  const [briefOpen, setBriefOpen] = useState(false);
  const [stripHost, setStripHost] = useState<HTMLDivElement | null>(null);
  const [historyRev, setHistoryRev] = useState<number | null>(null);
  const [historical, setHistorical] = useState<{ comparison: DocRevisionComparison; close(): void } | null>(null);
  const lastRead = useLastRead(receivedPlan.id, receivedPlan.rev);
  const plan = useLiveReview ? { ...receivedPlan, gate: liveReviewGate(receivedPlan.gate, liveState) } : receivedPlan;
  const teammate = receivedTeammate && useLiveReview ? { ...receivedTeammate, signedOff: !!receivedTeammate.me && !!liveState?.version?.signOffs.includes(receivedTeammate.me) } : receivedTeammate;
  const reviewVersion = useLiveReview ? liveState?.version?.id : undefined;
  const reviewBlocked = useLiveReview && reviewVersion === undefined;
  const visual = author?.visual;
  const canComment = !teammate || teammate.rights.comment;
  const canSuggest = !teammate || teammate.rights.suggest;
  // Picks are the person's to make: a teammate's is a vote, which the Library ties to commenting.
  const open = plan.state === "open";
  const canPick = open && canComment;
  const threads = useMemo(() => plan.threads ?? [], [plan.threads]);
  const sharedFiles = useMemo(() => overlaps(plan), [plan]);
  const [picks, setPicks] = useState<Record<string, string>>({});
  const draftScope = usePlanDraftScope();
  const [attached, setAttached] = useState<string[]>([]);
  const [uploading, setUploading] = useState(0);
  const [sending, setSending] = useState<"approve" | "changes" | "cancel" | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /* A write the network stopped: a calm note. Whether it landed can't be known (the request may
     have reached the Library before the answer timed out), so what the person drafted stays and the
     plan is read again. Try again goes through the control they used (`data-again`): it is offered
     only while that control is on screen and enabled, and clicking it runs that control with its
     own checks as the screen is now. A card's or thread's write keeps its own control instead. */
  const [net, setNet] = useState<LibraryNetwork | null>(null);
  const failedControl = useRef<string | null>(null);
  const [againName, setAgainName] = useState<string | null>(null);
  const [againControl, setAgainControl] = useState<HTMLButtonElement | null>(null);
  /* The plan was read back since the last failed write (null: no failed write is waiting on one). */
  const [readBack, setReadBack] = useState<boolean | null>(null);
  const verdict = useRef<HTMLElement | null>(null);
  const slow = useSlowPending(busy);
  const [picked, setPicked] = useState<Picked | null>(null);
  // What the person is writing belongs to the text it was started on (`source`): the doc stays
  // open across revisions, and a draft begun on one must not be sent against another's text.
  const [savedDraft, setDrafting, clearDraft] = usePlanDraft<{ draft: Draft; source: string; text: string; replacement: string | null }>("comment");
  const drafting = (savedDraft?.draft.kind === "suggest" ? canSuggest : canComment) ? savedDraft : null;
  const draft = drafting?.draft ?? null;
  const [box, setEditing] = useState<{ section: string; text: string; source: string } | null>(null);
  // A section's box belongs to Suggesting and Editing: back in Viewing (or decided), it is closed.
  const editing = mode === "viewing" ? null : box;
  const [followed, setFollowed] = useState<{ thread: string } | null>(null);
  const [tab, setTab] = useState<ReviewTab>("comments");
  const [collapsed, setCollapsed] = useState(storedCollapsed);
  // The planning doc (PlanDesignDoc.tsx): any plan on the design-doc spine, in every view of it
  // (solo, shared with reviewers, live, frozen for sign-off, or a teammate's). Only an older
  // free-form doc keeps the side column.
  const designDoc = onSpine(receivedPlan);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [cancelOpen, setCancelOpen] = useState(false);
  const [changesOpen, setChangesOpen] = useState(() => !!readPlanDraft<string>(draftScope, "verdict"));
  const doc = useRef<HTMLDivElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  // Team review: who can be asked, and where the doc can be shared. Absent (not on a Team plan,
  // or offline), the doc simply offers no sharing.
  const sharing: PlanSharingClient | undefined = client?.sharePlan && client.setPlanReviewers && client.syncPlan && client.signOffPlan && client.overridePlanPolicy && client.libraryPeople && client.docScopes ? (client as PlanSharingClient) : undefined;
  const [authorDirectory, setDirectory] = useState<LibraryPerson[]>([]);
  const directory = teammate?.directory ?? authorDirectory;
  const account = useAccount();
  const [adminOrg, setAdminOrg] = useState<string | null>(null);
  const [me, setMe] = useState<string | null>(null);
  const [scopes, setScopes] = useState<DocScope[]>([]);
  useEffect(() => {
    if (!sharing) return;
    sharing.libraryPeople().then((got) => { if (alive.current) { setDirectory(got.people); setAdminOrg(got.admin_org ?? null); setMe(got.me ?? null); } }, () => {});
    sharing.docScopes().then((got) => { if (alive.current) setScopes(got.scopes); }, () => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once for this doc: the reader is keyed by plan
  }, []);
  const gate = plan.gate ?? null;
  const shared = !!gate;
  const sharePeople = (!teammate || plan.can_edit) && plan.state !== "approved" ? source.people : undefined;
  const wasShared = useRef(shared);
  // Opening a doc in review brings back what teammates wrote; what changed arrives as an event.
  // Failing says nothing new: the gate shows what was last heard, and an approval asks again.
  useEffect(() => {
    const syncingInPanel = !wasShared.current && sharingOpen && !historical && sharePeople?.syncAccess;
    wasShared.current = shared;
    if (shared && plan.state !== "approved" && !syncingInPanel) sharing?.syncPlan(plan.id).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per doc, when it is known to be shared
  }, [plan.id, shared]);
  const nameOf = (who: string) => {
    const sub = who.replace(/^teammate:/, "");
    const ownName = sub === (teammate?.me ?? me) && account?.signed_in ? displayName(account) : "";
    const member = directory.find((p) => p.sub === sub);
    const saved = plan.teammate_names?.[sub];
    return ownName || displayName(member && { ...member, email: null }) || (saved !== "A teammate" && saved) || member?.email || "A teammate";
  };
  // A person in a sentence: "You", or their name's first word ("dana.okafor@…" → "Dana").
  const firstName = (who: string) => {
    if (who === "person" || (!!me && who === me) || who === teammate?.me) return "You";
    if (nameOf(who) === "A teammate") return "A teammate";
    const name = nameOf(who).split("@")[0].split(/[\s._-]+/)[0] ?? who;
    return name ? name[0].toUpperCase() + name.slice(1) : who;
  };
  const authorName = teammate ? (teammate.author ? firstName(teammate.author) : "the author") : "you";
  const approver = teammate ? (authorName === "You" || authorName === "A teammate" ? "the author" : authorName) : undefined;
  const gateWhy = [...(gate?.waiting_on ?? []), ...(gate?.reviewers ?? []).flatMap((r) => r.changes_by ?? [])].reduce((text, who) => text.split(who).join(nameOf(who)), gate?.reason ?? "");
  // Everyone the author chose has signed and only org rules are left.
  const unsigned = (gate?.reviewers ?? []).filter((r) => r.required && r.status !== "signed_off" && r.status !== "waived");
  const policyHeld = gate?.state === "waiting" && unsigned.length > 0 && unsigned.every((r) => !r.sub);
  const collapse = (to: boolean) => {
    setCollapsed(to);
    try {
      localStorage.setItem(REVIEW_COLLAPSED_KEY, to ? "1" : "0");
    } catch {
      // Not kept (storage is off or full): the column still collapses for now.
    }
  };

  // The doc's order: the overview, each section under its number, then each decision under its own.
  const numbers = new Map(plan.sections.map((s, i): [string, number] => [s.id, i + 1]));
  const figures = new Map(plan.sections.filter((s) => s.kind === "mermaid" || s.kind === "svg").map((s, i): [string, number] => [s.id, i + 1]));
  const textPlan: Plan = shownTexts ? {
    ...plan, lede: shownTexts.get(LEDE) ?? plan.lede,
    sections: plan.sections.map((s) => (s.kind === "mermaid" || s.kind === "svg" ? s : { ...s, markdown: shownTexts.get(s.id) ?? s.markdown })),
  } : plan;
  const bodyOf = (section: string) => (section === LEDE ? textPlan.lede : textPlan.sections.find((s) => s.id === section)?.markdown) ?? "";
  // What an anchor is on, as text: a draft is stale once this differs from what it was begun on.
  const decisionSource = (id: string) => {
    const d = plan.decisions.find((x) => x.id === id);
    return d ? JSON.stringify([d.title, d.mine, d.challenge, d.options]) : "";
  };
  const sourceOf = (a: PlanAnchor): string => {
    if (a.kind === "decision") return decisionSource(a.decision);
    if (a.kind === "section" && a.section.startsWith("decision:")) return decisionSource(a.section.slice(9));
    if (a.kind === "section" && a.section === "ask") return plan.ask ?? "";
    if (a.kind === "section" && a.section === "split") return JSON.stringify(plan.split.map((w) => [w.id, w.title, w.files, w.after, w.test]));
    return `${bodyOf(a.section)}\n${plan.sections.find((s) => s.id === a.section)?.caption ?? ""}`;
  };
  const anchorOf = (d: Draft): PlanAnchor => (d.kind === "suggest" ? { kind: "text", section: d.section, start: d.start, quote: d.before } : d.anchor);
  const draftStale = !!drafting && sourceOf(anchorOf(drafting.draft)) !== drafting.source;
  const editStale = !!editing && bodyOf(editing.section) !== editing.source;

  // A decision is settled for an open plan only after the person picks an option. A supervisor
  // recommendation is displayed, but does not count as the person's choice. A pick is saved as it
  // is made (`confirmed`), so one made before the doc was last closed still counts; `picks` holds
  // a pick on its way there.
  const valid = (d: PlanDecisionDTO, option: string | null | undefined) => (option && (d.options.some((o) => o.id === option) || d.answers?.some((a) => a.id === option)) ? option : undefined);
  const drafted = (d: PlanDecisionDTO) => valid(d, picks[d.id]) ?? valid(d, d.confirmed);
  const publicPick = (d: PlanDecisionDTO) => {
    const author = teammate?.author;
    const option = author && Object.keys(d.votes ?? {}).find((id) => d.votes?.[id]?.includes(author));
    return valid(d, d.confirmed) ?? valid(d, option);
  };
  const chosen = (d: PlanDecisionDTO) => open
    ? (canPick ? drafted(d) : publicPick(d)) ?? null
    : plan.verdict?.picks?.[d.id] ?? d.confirmed ?? d.mine ?? null;
  const settled = (d: PlanDecisionDTO) => !open || !!chosen(d);
  const next = canPick ? plan.decisions.find((d) => !settled(d)) : undefined;
  const decided = plan.decisions.filter(settled).length;
  const todo: TodoItem[] = plan.decisions.map((d) => {
    const label = settled(d) ? d.options.find((o) => o.id === chosen(d))?.label : undefined;
    return { id: d.id, title: d.title, state: settled(d) ? "done" : d === next ? "current" : "todo", pick: label && shortName(label) !== label ? shortName(label) : undefined };
  });
  const openThreads = threads.filter((t) => t.status === "open");
  const suggestions = openThreads.filter((t) => t.kind === "suggestion").length;

  // What a thread is on, as its card says it.
  const decisionName = (id: string | null | undefined) => `Decision · ${plan.decisions.find((d) => d.id === id)?.title ?? id}`;
  const partName = (section: string) => (section === "ask" ? "The brief" : section === LEDE ? "Overview" : section === "split" ? "The split"
    : section.startsWith("decision:") ? decisionName(section.slice(9)) : `§${numbers.get(section) ?? "?"} ${plan.sections.find((s) => s.id === section)?.title ?? section}`);
  // A thread's place in plain words: "§1 Summary", "Decision · Where cloud workers run", "Figure 1".
  const where = (t: PlanThreadDTO) => {
    const a = t.anchor;
    if (a.kind === "decision") return decisionName(a.decision);
    if (a.kind === "diagram") return `Figure ${figures.get(a.section ?? "") ?? "?"}`;
    return partName(a.section ?? "");
  };

  // The ranges drawn into a section's text: open comments' anchors with their numbers, open
  // suggestions' changes, and what is being written right now (the text commented on, or a
  // suggestion's change, tracked as it is typed).
  const marksOf = (section: string): Mark[] => {
    const out: Mark[] = [];
    const body = bodyOf(section);
    for (const t of openThreads) {
      const a = t.anchor;
      if (a.kind !== "text" || a.section !== section || a.state === "detached" || a.start == null || a.end == null) continue;
      // The engine's offsets count code points; the text on screen, UTF-16 units.
      const lit = followed?.thread === String(t.id);
      // The planning doc's notes sit beside their words, so the words carry no number and no colour.
      const range = designDoc ? { start: toUnits(body, a.start), end: toUnits(body, a.end), thread: t.id, lit, quiet: true }
        : { start: toUnits(body, a.start), end: toUnits(body, a.end), thread: t.id, ref: t.id, lit };
      // On the live or frozen text a range is only drawn where its words still are.
      if (shownTexts?.has(section) && a.quote != null && body.slice(range.start, range.end) !== a.quote) continue;
      out.push(t.kind === "suggestion" ? { ...range, kind: "del", ins: t.after ?? "" } : { ...range, kind: "anchor" });
    }
    // A suggestion is typed on the page in its own editor; a comment's words are lit while it is written.
    const on = draft && draft.kind !== "suggest" && !draftStale ? anchorOf(draft) : null;
    if (on?.kind === "text" && on.section === section) out.push({ start: on.start, end: on.start + on.quote.length, kind: "sel" });
    return out;
  };

  /* One write to the doc, and only one at a time: the engine answers with the plan as it is now. */
  const send = async (work: () => Promise<Plan>, refused?: (e: unknown) => void): Promise<Plan | null> => {
    if (!writes.begin()) return null;
    setBusy(true);
    setError(null);
    setNet(null);
    // The footer control this write came from, if any (a card's or thread's write has none).
    const control = failedControl.current;
    failedControl.current = null;
    setAgainName(null);
    setReadBack(null);
    try {
      const now = await work();
      writes.end(now);
      return now;
    } catch (e) {
      writes.end(null, (ok) => { if (alive.current) setReadBack(ok); });
      refused?.(e);
      if (alive.current) { setError(refusal(e)); setNet(libraryNetwork(e)); setAgainName(control); }
      return null;
    } finally {
      if (alive.current) setBusy(false);
    }
  };
  const write = async (work: () => Promise<Plan>) => (await send(work)) !== null;
  // After every render: the control to go through, only once the plan has been read back since the
  // failure (the write may have landed) and only while that control is on screen and enabled. An
  // action confirmed in a menu (Cancel planning, Request changes) goes through the menu: Try again
  // opens it, and the person confirms there.
  useEffect(() => {
    const root = verdict.current?.closest("[data-testid=\"plan-reader\"]") ?? verdict.current;
    const found = againName && net && readBack ? root?.querySelector<HTMLButtonElement>(`[data-again-menu="${againName}"]`)
      ?? root?.querySelector<HTMLButtonElement>(`[data-again="${againName}"]`) : null;
    const usable = found && !found.disabled ? found : null;
    setAgainControl((old) => (old === usable ? old : usable));
  });
  const goAgain = (control: HTMLButtonElement) => {
    if (!control.isConnected || control.disabled) return;
    if (control.dataset.againMenu && control.getAttribute("aria-expanded") === "true") return;   // already open
    control.click();
  };
  const footerNotes = (span: string) => <>
    {slow && <SlowConnectionNote className={`m-0 ${span} text-xs text-ink-3`} />}
    {error && net && <LibraryNetworkNote state={net} className={`m-0 flex ${span} items-center gap-2 text-xs text-ink-2`}
      hint="It couldn't be confirmed, so it may or may not have gone through. What you wrote is kept: check the plan before sending it again."
      buttonClassName={`${quiet} px-2 text-xs`}
      onTryAgain={busy ? undefined
        // The plan couldn't be read back yet: Try again reads it, and sends nothing.
        : readBack === false && writes.readAgain ? () => { setReadBack(null); writes.readAgain!(); }
        : againControl ? () => goAgain(againControl) : undefined} />}
  </>;
  // Own answers on a decision card (brindle/plan_answers.py). Adding one picks it and removing one
  // drops its pick, so the engine's `confirmed` speaks for the card from then on.
  const ownAnswers = (decision: string): OwnAnswers | undefined => {
    const own = source.answers;
    if (!own || historical || !canComment) return undefined;
    const repick = async (work: () => Promise<Plan>) => {
      const saved = await write(work);
      if (saved && alive.current) setPicks((p) => Object.fromEntries(Object.entries(p).filter(([k]) => k !== decision)));
      return saved;
    };
    return {
      add: (text) => repick(() => own.add(plan.rev, decision, text)),
      edit: (answer, text) => write(() => own.edit(plan.rev, answer, text)),
      remove: (answer) => repick(() => own.remove(plan.rev, answer)),
    };
  };
  const actions: ThreadActions = {
    reply: (thread, text) => write(() => source.reply(plan.rev, thread, text)),
    resolve: (thread, resolved) => write(() => source.resolve(plan.rev, thread, resolved)),
    accept: (thread) => (client ? write(() => client.acceptPlanSuggestion(plan.id, thread, plan.rev)) : Promise.resolve(false)),
    reject: (thread) => (client ? write(() => client.rejectPlanSuggestion(plan.id, thread)) : Promise.resolve(false)),
  };

  // A highlight in the doc: where it is in its section's own text, and where to offer the bar.
  const pick = () => {
    const root = doc.current;
    const selection = window.getSelection();
    if (!root || !selection || selection.rangeCount === 0 || selection.isCollapsed || (!canComment && !canSuggest)) return setPicked(null);
    const range = selection.getRangeAt(0);
    const rect = range.getClientRects?.()[0];
    const host = root.getBoundingClientRect();
    const at = { x: Math.max(8, Math.min((rect?.left ?? host.left) - host.left + root.scrollLeft, root.clientWidth - 320)), y: (rect?.top ?? host.top) - host.top + root.scrollTop };
    const text = selectedRange(root, selection);
    if (text) {
      const quote = bodyOf(text.section).slice(text.start, text.end);
      return setPicked(quote.trim() ? { ...at, kind: "text", section: text.section, start: text.start, quote } : null);
    }
    const node = range.commonAncestorContainer;
    const figure = (node instanceof Element ? node : node.parentElement)?.closest<HTMLElement>("[data-diagram]");
    setPicked(figure?.dataset.diagram ? { ...at, kind: "diagram", section: figure.dataset.diagram } : null);
  };
  const begin = (next: Draft) => {
    setDrafting({ draft: next, source: sourceOf(anchorOf(next)), text: "", replacement: next.kind === "suggest" ? bodyOf(next.section) : null });
    setPicked(null);
    setTab("comments");
    window.getSelection()?.removeAllRanges();
  };
  // Suggesting is typed on the page: the section opens in its suggestion editor, with the
  // highlighted words (or a caret) selected, and Suggesting is the mode while it is.
  const suggestAt = (section: string, start: number, quote: string) => {
    if (chrome && mode !== "suggesting" && chrome.allowed("suggesting")) chrome.onMode("suggesting");
    begin({ kind: "suggest", section, start, before: quote, where: partName(section) });
  };
  const offer = (kind: "comment" | "ask" | "suggest") => {
    if (!picked) return;
    if (picked.kind === "diagram") return begin({ kind: kind === "ask" ? "ask" : "comment", anchor: { kind: "diagram", section: picked.section }, where: `Figure ${figures.get(picked.section) ?? "?"}` });
    const { section, start, quote } = picked;
    if (kind === "suggest") return suggestAt(section, start, quote);
    begin({ kind, anchor: { kind: "text", section, start, quote }, where: partName(section) });
  };
  // "Create work item": an item that quotes the highlighted passage and names its section.
  const createFromPassage = () => {
    if (!picked || picked.kind !== "text" || !work) return;
    const { section, quote } = picked;
    setPicked(null);
    if (collapsed) collapse(false);
    setTab("tasks");
    void work.create({ kind: "passage", section, quote });
  };
  const comment = (anchor: PlanAnchor, on: string) => begin({ kind: "comment", anchor, where: on });
  const draftText = drafting?.replacement ?? null;
  const setDraftText = (replacement: string) => { if (drafting) setDrafting({ ...drafting, replacement }); };
  const setDraftReason = (text: string) => { if (drafting) setDrafting({ ...drafting, text }); };
  // What the suggestion being typed changes: one replaced range of its section (start in code points).
  const draftChange = draft?.kind === "suggest" && draftText !== null ? changeBetween(bodyOf(draft.section), draftText) : null;
  // A suggestion is typed only in Suggesting, on text that isn't locked; a locked version takes none.
  const suggestOpen = mode === "suggesting" && !locked;
  const suggestionReady = !locked && !!draftChange && (!!draftChange.before || !!draftChange.after);
  // The draft's offsets are the screen's (UTF-16); the engine is sent code points. A draft whose
  // text changed under it (a newer revision) isn't sent: its range no longer says what was meant.
  const sendDraft = async (text: string) => {
    if (!draft || draftStale) return;
    if (draft.kind === "suggest" && draftChange && (draftChange.before || draftChange.after)) {
      if (!suggestionReady) return;
      if (await write(() => source.suggest(plan.rev, { section: draft.section, ...draftChange }, text))) clearDraft();
      return;
    }
    if (!text.trim()) return;
    const on = anchorOf(draft);
    const start = on.kind === "text" ? toPoints(bodyOf(on.section), on.start) : 0;
    if (await write(() => source.comment(plan.rev, on.kind === "text" ? { ...on, start } : on, text, draft.kind === "ask"))) clearDraft();
  };

  // A section's box, saved: only what changed between the text it was opened on and the box is
  // sent. If that text has changed since (a newer revision), nothing is: the box's draft was
  // written against the older text and would put it back.
  const saveEdit = async () => {
    if (!editing || editStale) return;
    const change: PlanChange = { section: editing.section, ...changeBetween(editing.source, editing.text) };
    const sent = mode === "editing" && client ? () => client.editPlan(plan.id, plan.rev, change) : () => source.suggest(plan.rev, change);
    if ((await write(sent)) && alive.current) setEditing(null);
  };

  // Following a thread to its place in the doc, and an underlined anchor to its thread.
  const showThread = (t: PlanThreadDTO) => {
    setFollowed({ thread: String(t.id) });
    const a = t.anchor;
    const root = doc.current;
    show(root?.querySelector(`[data-thread="${t.id}"]`) ?? document.getElementById(a.kind === "decision" ? `plan-decision-${a.decision}` : `plan-${a.section?.replace(/^decision:/, "decision-") ?? "ask"}`), "center");
  };
  const followAnchor = (e: MouseEvent) => {
    const at = (e.target as HTMLElement).closest<HTMLElement>("[data-thread],[data-ref]");
    const thread = at?.dataset.thread ?? at?.dataset.ref;
    if (!thread || (collapsed && !designDoc)) return;
    setTab("comments");
    setFollowed({ thread });
  };
  useEffect(() => {
    if (followed) show(document.querySelector(`[data-testid="thread-${followed.thread}"]`), "nearest");
  }, [followed]);
  const jump = (decision: string) => show(document.getElementById(`plan-decision-${decision}`));
  // A pick is saved as it is made (the author's on this machine, and through the Library to
  // everyone on a shared plan; a teammate's is their vote there), so closing the doc before
  // Approve loses nothing. Each pick is its own write, sent at once, and one at a time: while one
  // is out the cards take no pick, so no pick ever waits on this screen to be sent. The author's
  // shows at once (and goes back to what was there if it isn't saved); a teammate's counts as
  // decided only once the Library has it.
  const pickOption = async (decision: string, option: string) => {
    if (!canPick || historical || busy) return;
    const before = picks[decision];
    if (!teammate) setPicks((p) => ({ ...p, [decision]: option }));
    const saved = await write(() => source.vote(plan.rev, decision, option));
    if (!alive.current) return;
    if (saved) setPicks((p) => ({ ...p, [decision]: option }));
    else if (!teammate) setPicks((p) => (p[decision] === option ? { ...p, [decision]: before } : p));
  };

  // The bottom bar's actions.
  const target = visual?.answer_agent_id;
  const canAttach = open && !!client?.attach && !!target && visual?.accepts_files === true;
  const upload = async (files: File[]) => {
    setUploading((n) => n + 1);
    setError(null);
    setNet(null);
    try {
      for (const file of files) {
        const got = await client!.attach!(target!, await fileAttachment(file));
        if (!alive.current) return;
        setAttached((list) => [...list, ...got.paths]);
      }
    } catch (e) {
      if (alive.current) setError(`Couldn't attach the file: ${message(e)}`);
    } finally {
      if (alive.current) setUploading((n) => n - 1);
    }
  };
  // What the supervisor hasn't had: the person's open threads, and their replies, each of which
  // the engine marks `sent` once a verdict has carried it.
  const proposals = (plan.revisions ?? []).filter((r) => r.author === "supervisor");
  const feedback = threads.filter((t) => !t.question);
  const myReplies = feedback.flatMap((t) => t.replies ?? []).filter((r) => r.author === "person");
  const pending = feedback.some((t) => t.author === "person" && t.status === "open" && !t.sent) || myReplies.some((r) => !r.sent);
  const lastProposal = Math.max(0, ...proposals.map((r) => r.rev));
  const edited = (plan.revisions ?? []).some((r) => r.author === "person" && r.rev > lastProposal);
  const differs = plan.decisions.some((d) => (drafted(d) ?? d.confirmed ?? d.mine) !== d.mine);
  const hasFeedback = pending || edited || differs;
  const drafts = Object.fromEntries(plan.decisions.flatMap((d) => (drafted(d) ? [[d.id, drafted(d)!]] : [])));
  /* `shared`: false once the person stopped sharing it, when it is approved as a personal doc, with no
     frozen version to wait for or to name. */
  const decide = async (approved: boolean, shared = true) => {
    if (historical || !client || (approved && shared && reviewBlocked)) return;
    failedControl.current = approved ? "approve" : "changes";
    setSending(approved ? "approve" : "changes");
    const version = shared ? reviewVersion : undefined;
    const note = readPlanDraft<string>(draftScope, "verdict");
    const now = await send(() => client.decidePlan(plan.id, { rev: plan.rev, approved, picks: drafts, comments: [], note: note?.trim() ?? "", ...(version === undefined ? {} : { version }), ...(attached.length ? { attachments: attached } : {}) }));
    if (now && readPlanDraft(draftScope, "verdict") === note) savePlanDraft(draftScope, "verdict", null);
    if (!alive.current) return;
    setSending(null);
    if (now) {
      // What was drafted went with the verdict: the next revision is a fresh read.
      setAttached([]);
      setPicks({});
    }
  };
  /* Close the plan without approving it: nothing is built or sent to the Library, and the tasks it held wait for the supervisor. */
  const cancelPlanning = async () => {
    if (historical || !client || !client.cancelPlan) return;
    failedControl.current = "cancel";
    setSending("cancel");
    const now = await send(() => client.cancelPlan!(plan.id, plan.rev));
    if (now) savePlanDraft(draftScope, "verdict", null);
    if (alive.current) setSending(null);
  };
  /* The plan becomes a personal doc, then the person's sign-off starts the build. */
  /* Built only when the personal doc it becomes reads as the text on screen (the live draft's, or a
     frozen version's). If it doesn't, nothing is built: the personal revision is shown, said so, for
     the person to read and Approve. */
  const stopSharing = async () => {
    if (historical) return;
    failedControl.current = "stop";
    const read = shownTexts;
    setSending("approve");
    const now = await send(() => sharing!.stopSharingPlan!(plan.id, plan.rev));
    if (now && read && [...read].some(([id, text]) => (id === LEDE ? now.lede : now.sections.find((s) => s.id === id)?.markdown) !== text)) {
      if (alive.current) { setSending(null); setPersonalDiffers(true); }
      return;
    }
    if (now) await decide(true, false);
    else if (alive.current) setSending(null);
  };
  const signOff = async () => {
    if (historical || !source.signOff || reviewBlocked) return;
    failedControl.current = "signoff";
    setSending("approve");
    if (await write(() => source.signOff!(plan.rev, reviewVersion)) && frozen) setRefreshes((n) => n + 1);
    if (alive.current) setSending(null);
  };
  // A write that may change the review (the Share panel's sign-off) asks for the frozen version again.
  const reviewWrite = async (work: () => Promise<Plan>) => {
    if (historical) return false;
    const done = await write(work);
    if (done && frozen) setRefreshes((n) => n + 1);
    return done;
  };
  const signers = (gate?.reviewers ?? []).filter((r) => r.sub);
  const iAmUnsigned = signers.some((r) => (r.you || (!!teammate?.me && r.sub === teammate.me)) && r.required && r.status !== "signed_off");
  const unsignedOthers = unsigned.filter((r) => !(r.you || (!!teammate?.me && r.sub === teammate.me)));
  // The next step in words, not a run of counts.
  const summary = [
    plan.decisions.length - decided > 0 && `${plural(plan.decisions.length - decided, "decision")} left.`,
    gate && useLiveReview && !frozen && plan.state === "open" && (teammate ? `You sign once ${authorName} freezes a version.` : "Freeze a version when the text is ready for sign-off."),
    gate && frozen && iAmUnsigned && unsignedOthers.length === 0 && `You are the last required reviewer.${teammate ? ` ${authorName} can start the build once you sign.` : ""}`,
    gate && frozen && unsignedOthers.length > 0 && `Waiting on ${unsignedOthers.map((r) => (r.sub ? firstName(r.sub) : r.name ?? "an org rule")).join(", ")}.`,
    gate && !useLiveReview && gate.required > 0 && `${gate.signed} of ${plural(gate.required, "required sign-off")}.`,
    suggestions > 0 && !teammate && `${plural(suggestions, "suggestion")} to accept.`,
    openThreads.length - suggestions > 0 && `${plural(openThreads.length - suggestions, "comment")} open.`,
    plan.queued > 0 && `${plural(plan.queued, "assigned task waits", "assigned tasks wait")} on this plan.`,
  ].filter(Boolean).slice(0, 2).join(" ");
  const signName = frozen ? `Sign off version ${frozen.id}` : "Sign off";

  // A text section: its text with the threads' ranges drawn in, or, being edited, its text where
  // it reads (live) or its markdown in a box.
  const commentButton = (on: () => void, name = "Comment") => canComment && <button type="button" className={`${quiet} -my-0.5 text-xs`} onClick={on}>{name}</button>;
  // Editing opens a section where it reads; Suggesting is done on a highlight, as a tracked change.
  const editControl = (section: string, title: string) => mode === "editing" && editing?.section !== section && (
    <button type="button" className={`${quiet} -my-0.5 text-xs`} aria-label={`Edit ${title}`} disabled={!!editing} onClick={() => setEditing({ section, text: bodyOf(section), source: bodyOf(section) })}>
      Edit
    </button>
  );
  // A doc in team review, in Editing mode, is edited live with everyone writing in it: its text
  // is the daemon's live document (DocEditor), each change going out as it is typed.
  const live = mode === "editing" && useLiveReview ? docLive : null;
  // What teammates see beside this person's caret and in the doc's presence.
  const myName = personName(teammate?.me ? nameOf(teammate.me) : me ? nameOf(me) : "Author");
  const liveEditor = (section: string, title: string, variant: "page" | "source") => live && (
    <div className="grid font-sans" data-testid="plan-live-box">
      <Suspense fallback={<p className="text-xs text-ink-3" role="status">Opening the live text…</p>}>
        <DocEditor client={live} visual={plan.id} section={section} name={myName} label={`Text of ${title}`} isAuthor={!!author} variant={variant} />
      </Suspense>
    </div>
  );
  const editBox = (title: string) => editing && (
    <div className="grid gap-2 font-sans" data-testid="plan-edit-box" data-stale={editStale || undefined}>
      <FormatTextarea className={`${field} w-full resize-y font-mono text-[12.5px] leading-[1.6]`} aria-label={`Text of ${title}`} autoFocus rows={Math.min(24, editing.text.split("\n").length + 2)}
        value={editing.text} onValue={(text) => setEditing({ ...editing, text })} />
      {editStale && <p role="alert" className="text-xs text-sap">This section changed in a newer revision, so this draft can't be saved over it. Copy what you need, cancel, and edit the new text.</p>}
      <div className="flex items-center gap-1.5">
        <LoadingButton className={`${button} text-xs`} busy={busy} disabled={busy || editStale || editing.text === editing.source} onClick={() => void saveEdit()}>
          Save
        </LoadingButton>
        <button type="button" className={`${quiet} text-xs`} onClick={() => setEditing(null)}>Cancel</button>
      </div>
    </div>
  );
  // Where a click lands in a section's text (its offset in the source), for a suggestion's caret.
  const caretIn = (section: string) => {
    const selection = window.getSelection();
    const node = selection?.anchorNode;
    const leaf = (node instanceof Element ? node : node?.parentElement)?.closest<HTMLElement>("[data-o]");
    return leaf ? Number(leaf.dataset.o) + (selection?.anchorOffset ?? 0) : bodyOf(section).length;
  };
  const opened = (section: string) => {
    if (window.getSelection()?.toString() || editing || draft) return;
    if (mode === "editing") setEditing({ section, text: bodyOf(section), source: bodyOf(section) });
    else if (mode === "suggesting" && canSuggest && plan.state !== "approved") suggestAt(section, caretIn(section), "");
  };
  const textBody = (section: string, title: string) => editing?.section === section ? (live ? liveEditor(section, title, "page") : editBox(title))
    : draft?.kind === "suggest" && draft.section === section && !draftStale && suggestOpen ? (
      <div className="grid font-sans" data-testid="plan-suggest-box">
        <Suspense fallback={<DocText body={bodyOf(section)} />}>
          <SuggestEditor key={`${drafting!.source}:${draft.start}`} source={bodyOf(section)} text={draftText ?? undefined} select={{ from: draft.start, to: draft.start + draft.before.length }}
            label={`Suggestion to ${title}`} onText={setDraftText} />
        </Suspense>
      </div>
    ) : draft?.kind === "suggest" && draft.section === section && !draftStale && draftChange ? (
      // Out of Suggesting (another mode, or the text locked for sign-off): the change, read only.
      <div data-section={section} className="grid gap-2.5" data-testid="plan-suggest-preview">
        <DocText body={bodyOf(section)} marks={[...marksOf(section), { start: toUnits(bodyOf(section), draftChange.start), end: toUnits(bodyOf(section), draftChange.start) + draftChange.before.length, kind: "del", ins: draftChange.after }]} />
      </div>
    ) : <SectionText section={section} body={bodyOf(section)} marks={marksOf(section)} layout={designDoc ? docLayout(section) : undefined} approver={approver}
      onOpen={mode === "viewing" ? undefined : opened} />;
  // The section being edited live wears a blue rule in the margin.
  const editingRule = "relative before:absolute before:-left-10 before:bottom-0 before:top-3.5 before:w-0.5 before:rounded-sm before:bg-[var(--group-1)]";
  const section = (s: PlanSectionDTO): ReactNode => {
    const diagram = s.kind === "mermaid" || s.kind === "svg";
    const here = editing?.section === s.id;
    return (
      <section key={s.id} id={`plan-${s.id}`} aria-labelledby={`plan-title-${s.id}`} className={`grid gap-2.5 border-t pt-4 ${here ? `border-[var(--group-1)] ${editingRule}` : "border-line"}`}>
        <h3 className={heading}>
          <span aria-hidden className={margin}>{numbers.get(s.id)}</span>
          <span id={`plan-title-${s.id}`}>{s.title}</span>
          <span className={`${aside} ${here ? "!text-[var(--group-1)]" : ""}`}>
            {here ? (live ? "You are writing here · live" : "You are editing") : !diagram && editControl(s.id, s.title)}
          </span>
        </h3>
        {diagram ? (
          <Diagram section={s} figure={figures.get(s.id) ?? 1}>
            {commentButton(() => comment({ kind: "diagram", section: s.id }, `Figure ${figures.get(s.id)}`))}
          </Diagram>
        ) : textBody(s.id, s.title)}
      </section>
    );
  };
  const columnOpen = !collapsed || !!draft;
  // What the person can type into, so the formatting toolbar shows: a section open for editing,
  // a suggestion's new text, or the Markdown view while Editing live.
  const typing = !locked && (mode === "editing" || (mode === "suggesting" && draft?.kind === "suggest"));
  const markdownState = locked ? "locked" : mode === "editing" && live ? "editing" : "viewing";
  // The one number in the toolbar: the frozen version while there is one, else the revision.
  const versionLabel = frozen ? `Version ${frozen.id}` : `rev ${plan.rev}`;
  // Freeze, reopen and sign-off can add timeline entries within a revision.
  const historyVersion = JSON.stringify([
    plan.rev, plan.state, frozen?.id, frozen?.signOffs, frozen?.optionalSignOffs, teammate?.signOffs,
    gate?.reviewers?.map((reviewer) => [reviewer.sub, reviewer.status]),
  ]);
  const reviewerNames = signers.filter((r) => r.required).map((r) => (r.sub ? firstName(r.sub) : r.name ?? "")).filter(Boolean);

  // The status line.
  const closed = plan.state === "cancelled" || plan.state === "closed";
  const stage = plan.state === "closed" ? "Closed" : plan.state === "writing" ? "Being written" : plan.state === "superseded" ? "Superseded" : closed ? "Cancelled"
    : !open && !teammate ?(plan.state === "approved" ? (gate ? "Review closed" : "Signed off") : "Changes requested")
    : frozen ? "Locked for sign-off"
    : gate ? (gate.state === "ready" ? "Ready to build" : gate.state === "changes_requested" ? "Changes requested" : policyHeld ? "Blocked by org policy" : "In team review") : "Needs your sign-off";
  const accessLine = plan.can_edit ? "You can edit" : canComment ? "You can comment" : "You can view";
  const statusLine = teammate ? accessLine : plan.state === "writing" ? "The supervisor is writing this plan"
    : plan.state === "closed" ? "Closed: its session ended"
    : plan.state === "superseded" ? "You asked for a newer plan; this one is closed"
    : plan.state === "cancelled" ? "Closed without approval; the tasks it held wait for the supervisor"
    : frozen && gate ? `Version ${frozen.id} · ${gate.signed} of ${plural(gate.required, "sign-off")}`
    : gate && useLiveReview && open ? `Still being written · sign-off opens when ${teammate ? authorName : "you"} freeze${teammate ? "s" : ""} a version`
    : !open ? (plan.state === "approved" ? "Its workers are building it" : "The supervisor is revising it")
    : gate ? "Build starts when every required reviewer signs off" : "Nothing is built until you sign off";
  const changedAt = Math.max(plan.created_at, ...(plan.revisions ?? []).map((r) => r.at));
  const byline = `by ${teammate ? (teammate.author ? personName(nameOf(teammate.author)) : "its author") : "you and your supervisor"} · changed ${ago(changedAt)}`;

  // The sign-off sheet, under the decisions: once a version is frozen (or for a doc reviewed without live text).
  const sheetRows: SignOffRow[] = signers.map((r) => ({
    key: r.sub!, who: r.you ? "person" : nameOf(r.sub!), name: r.you ? "You" : personName(nameOf(r.sub!)), required: r.required,
    state: r.status === "signed_off" ? "signed" : r.status === "changes_requested" ? "changes" : r.status === "waived" ? "waived" : "waiting",
  }));
  const sheetRev = frozen?.id ?? plan.rev;
  const optionalSigners = frozen?.optionalSignOffs ?? (teammate?.signOffs ?? [])
    .filter((r) => r.rev === sheetRev && r.optional).map((r) => r.sub);
  const liveSigners = frozen && new Set(frozen.signOffs);
  const listed = new Set(sheetRows.map((r) => r.key));
  for (const sub of optionalSigners) {
    if (listed.has(sub) || (liveSigners && !liveSigners.has(sub))) continue;
    const you = sub === teammate?.me;
    const name = nameOf(sub);
    sheetRows.push({ key: sub, who: you ? "person" : name, name: you ? "You" : personName(name), required: false, state: "signed" });
    listed.add(sub);
  }
  if (teammate?.me && teammate.signOff === "optional" && sheetRev === plan.rev && !listed.has(teammate.me)) {
    sheetRows.push({ key: teammate.me, who: "person", name: "You", required: false, state: teammate.signedOff ? "signed" : "waiting" });
  }
  const sinceRevs = lastRead !== null && lastRead < plan.rev ? (plan.revisions ?? []).filter((r) => r.rev > lastRead).map((r) => ({ rev: r.rev, sections: (r.changed ?? []).map((id) => partName(id)) })) : null;
  const sheet = gate && (frozen || !useLiveReview) ? (
    <SignOffSheet version={frozen ? `version ${frozen.id}` : `rev ${plan.rev}`} rows={sheetRows} since={sinceRevs && lastRead !== null ? { from: lastRead, changes: sinceRevs } : null} onShowChanges={() => setTab("history")} />
  ) : undefined;

  // The first share happens in the panel too: adding a person or a team shares the plan.
  const canShare = !!gate || (!!sharing && !!sharePeople && plan.state !== "approved" && scopes.some((s) => s.kind !== "user" && s.can_share !== false));
  const previewRules = useMemo(() => sharing?.previewPlanPolicies && (() => sharing.previewPlanPolicies!(plan.id).then((r) => r.policies)),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the client is fixed for this doc
    [plan.id]);
  // The link: Pro and up (the daemon says `available`), for whoever may edit the design.
  const canOpenShare = canShare || (!!shareLink && (!!link.state?.available || !!link.blocked));
  const shareMenu = canOpenShare && <button type="button" aria-label="Share" title="Share" className={`${button} size-7 shrink-0 !p-1.5 ${sharingOpen ? "!border-transparent bg-branch-soft text-branch" : ""}`}
    aria-expanded={sharingOpen} onClick={() => setSharingOpen((o) => !o)}><svg className="size-4" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden><circle cx="6" cy="5" r="2.5" /><path d="M1.5 14v-2a4.5 4.5 0 019 0v2M11 3a2.5 2.5 0 010 5m2 6v-2a4 4 0 00-1-2.5" /></svg></button>;
  const ownReviewer = signers.find((r) => r.you && r.status !== "signed_off");
  const sharePanel = canOpenShare && <SharePanel plan={plan} target={sharePeople}
    signOff={sharing && ownReviewer && plan.state !== "approved" ? { sub: ownReviewer.sub!, disabled: busy || reviewBlocked,
      run: () => void reviewWrite(() => sharing.signOffPlan(plan.id, plan.rev, reviewVersion)) } : undefined}
    nameOf={nameOf} writes={writes} onBusy={setBusy} visible={sharingOpen && !historical} onClose={() => setSharingOpen(false)}
    teams={scopes} owned={!teammate} previewRules={gate ? undefined : previewRules} onStopAll={() => setUnshared((n) => n + 1)}>
    {!!gate?.policies?.length && <section className="sec"><Policies plan={plan} client={sharing} directory={directory} adminOrg={adminOrg} me={me} busy={busy} nameOf={nameOf} write={reviewWrite} /></section>}
    {shareLink && <ShareLinkSection target={shareLink} link={link} />}
  </SharePanel>;
  const presence = docLive && (
    <DocPresence client={docLive} visual={plan.id} isAuthor={!!author} name={myName} onState={setLiveState} onCapability={setLiveCapability} onText={setLiveTexts} refreshes={refreshes}
      mode={mode} stripHost={stripHost} reviewers={reviewerNames} onHistory={designDoc ? () => setHistoryOpen(true) : () => { if (collapsed) collapse(false); setTab("history"); }} />
  );
  // What the Library has yet to confirm about a shared plan, under its status line.
  const gateNotes = (
    <>
      {open && gate?.unconfirmed && (
        <div className="grid gap-1 font-sans text-[12.5px] text-ink-2" data-testid="plan-unconfirmed" data-reason={gate.unconfirmed.reason} data-held={gate.unconfirmed.approval_held || undefined}>
          <span>{gate.unconfirmed.message}</span>
          {gate.unconfirmed.approval_held && <span>Your approval is saved. The build starts by itself once the Library confirms this revision is ready.</span>}
          {(gate.unconfirmed.reason === "sign_in" || gate.unconfirmed.reason === "other_account") && <LibrarySignIn />}
          {gate.unconfirmed.can_stop_sharing && !next && sharing?.stopSharingPlan && (
            <span>
              <button type="button" className={`${quiet} -my-0.5 text-xs`} disabled={busy} title="Your teammates lose this plan the next time brindle reaches the Library" data-again="stop" onClick={() => void stopSharing()}>Stop sharing and build</button>
            </span>
          )}
        </div>
      )}
      {open && gate?.approval_refused && <p className="m-0 font-sans text-[12.5px] text-sap" data-testid="plan-approval-refused">{gate.approval_refused}</p>}
      {open && !gate && personalDiffers && (
        <p role="status" className="m-0 font-sans text-[12.5px] text-sap" data-testid="plan-personal-differs">
          Sharing stopped, but nothing was built: the plan kept here reads differently from the shared text you were reading. Read it as it is now, then Approve to build it.
        </p>
      )}
    </>
  );
  // What a highlight offers, over the words.
  const highlightBar = picked && !historical && (
    <div role="toolbar" aria-label="For the highlighted text" className="absolute z-10 inline-flex -translate-y-[calc(100%+6px)] gap-0.5 rounded-md border border-line-strong bg-canvas-2 p-[3px] font-sans text-xs shadow-lg" style={{ left: picked.x, top: picked.y }}
      onMouseDown={(e) => e.preventDefault()} onMouseUp={(e) => e.stopPropagation()}>
      {canComment && <button type="button" className="cursor-pointer rounded-[3px] bg-branch-soft px-[9px] py-0.5 font-medium text-branch" onClick={() => offer("comment")}>Comment</button>}
      {picked.kind === "text" && plan.state !== "approved" && canSuggest && <button type="button" className="cursor-pointer rounded-[3px] px-[9px] py-0.5 text-ink-2 hover:bg-surface-2" onClick={() => offer("suggest")}>Suggest edit</button>}
      {canComment && <button type="button" className="cursor-pointer rounded-[3px] px-[9px] py-0.5 text-ink-2 hover:bg-surface-2" onClick={() => offer("ask")}>{teammate?.author ? `Ask ${authorName}` : "Ask the supervisor"}</button>}
      {picked.kind === "text" && work && Object.values(work.state?.connected ?? {}).some(Boolean) && <button type="button" className="cursor-pointer rounded-[3px] px-[9px] py-0.5 text-ink hover:bg-surface-2" onClick={createFromPassage}>Create work item</button>}
    </div>
  );

  // The bottom bar while a suggestion is typed, while editing live, and once the plan is decided.
  const suggestingBar = (
    <>
      <div className="grid min-w-0 gap-0.5" data-testid="plan-next" data-state="suggesting">
        <span className="flex items-center gap-2.5 text-sm font-semibold text-ink">
          <span className={`${label} text-[11px]`}>Suggesting</span>
          <span className="truncate">{suggestionReady ? "1 change not sent" : "Type your change in the plan"}</span>
        </span>
        <span className="text-xs text-ink-3">{teammate ? `Your edits are suggestions. ${authorName} accepts or rejects each one.` : "Your edits are suggestions. You accept or reject each one."}</span>
      </div>
      <div className="flex items-center gap-2">
        <button type="button" className={quiet} onClick={() => setDrafting(null)}>Discard all</button>
        <button type="button" className={main} disabled={busy || draftStale || !suggestionReady}
          onClick={() => doc.current?.ownerDocument.querySelector<HTMLFormElement>('[data-testid="thread-draft"]')?.requestSubmit()}>Send 1 suggestion</button>
      </div>
    </>
  );
  const editingBar = (
    <>
      <div className="grid min-w-0 gap-0.5" data-testid="plan-next" data-state="editing">
        <span className="flex items-center gap-2.5 text-sm font-semibold text-ink">
          <span className={`${label} text-[11px]`}>Editing</span>
          <span className="truncate">{raw ? "The Markdown" : editing ? partName(editing.section) : ""}</span>
        </span>
        <span className="text-xs text-ink-3">Sign-offs wait for a frozen version: nobody signs text that is still moving.</span>
      </div>
      <div className="flex items-center gap-2">
        <button type="button" className={button} onClick={() => { setEditing(null); chrome?.onMode("viewing"); }}>Done editing</button>
      </div>
    </>
  );
  const decidedBar = (
    <>
      {/* A redirected plan stays open to cancel (never to approve): the person may close it instead of waiting for a revision. */}
      {plan.state === "redirected" && client?.cancelPlan && (
        <div className="col-span-full flex justify-end">
          <LoadingButton className={quiet} busy={sending === "cancel"} disabled={!!historical || busy} data-testid="plan-cancel" data-again="cancel" onClick={() => void cancelPlanning()}>Cancel planning</LoadingButton>
        </div>
      )}
      <div role="status" className="col-span-full flex min-w-0 flex-col gap-0.5 text-sm" data-testid="plan-decided" data-state={plan.state}>
        {plan.state === "closed"
          ? <p data-testid="plan-closed">Closed: its session ended</p>
          : plan.state === "cancelled"
          ? <p data-testid="plan-closed">This planning is closed without approval. The tasks it held wait for the supervisor's decision on each.</p>
          : <p>{plan.state === "approved" ? `You signed off r${plan.rev}. Its workers start now.` : plan.state === "writing" ? "The supervisor is writing this plan." : plan.state === "superseded" ? "A newer plan replaced this one." : "You asked for changes. The supervisor is revising the plan."}</p>}
        {plan.verdict?.note && <p className="text-ink-2"><span className="text-ink-3">your note · </span>{plan.verdict.note}</p>}
        {plan.verdict?.comments?.map((c, i) => (
          <p key={i} className="text-ink-2">
            <span className="text-ink-3">on {partName(c.section)} · </span>
            {c.text}
          </p>
        ))}
      </div>
    </>
  );
  /* The planning doc (design: plan-doc-redesign.html): the outline, the serif page with each
     decision inline at its section and the comments in the margin, and one bar along the bottom. */
  function planningDoc() {
    const parts = spineParts(textPlan.sections);
    const placed = placeDecisions(parts, plan.decisions);
    const ordered = decisionsInOrder(parts, placed);
    const waiting = (d: PlanDecisionDTO) => open && !settled(d);
    const left = canPick ? ordered.filter(waiting) : [];
    const sectionIds = new Set(plan.sections.map((s) => s.id));
    // Where a note sits: beside its words, its section, its decision's card, or the overview.
    const home = (a: PlanAnchor | PlanThreadDTO["anchor"], thread?: number): { at: string; anchor: string | null } => {
      const section = "section" in a ? a.section ?? "" : "";
      if (a.kind === "decision") return { at: `decision:${a.decision}`, anchor: "[data-testid=plan-decision]" };
      if (section.startsWith("decision:")) return { at: section, anchor: "[data-testid=plan-decision]" };
      if (!sectionIds.has(section)) return { at: LEDE, anchor: null };
      if (a.kind === "text" && !("state" in a && a.state === "detached")) return { at: section, anchor: thread === undefined ? "[data-sel]" : `[data-thread="${thread}"]` };
      return { at: section, anchor: null };
    };
    const draftAt = draftStale ? { at: LEDE, anchor: null }
      : draft ? (draft.kind === "suggest" ? { at: draft.section, anchor: null } : home(draft.anchor)) : null;
    const notesFor = (at: string) => [
      ...openThreads.map((t) => ({ t, h: home(t.anchor, t.id) })).filter(({ h }) => h.at === at).map(({ t, h }) => ({
        key: `t${t.id}`, anchor: h.anchor, offset: t.anchor.start,
        // A teammate's suggestions are the author's to judge, and a teammate resolves only their own comments.
        node: <MarginNote thread={t} busy={busy} canJudge={!teammate && plan.state === "open"} canReply={canComment} canResolve={!teammate || (canComment && t.author === "person")} actions={actions} lit={followed?.thread === String(t.id)}
          supervisorReplying={isSupervisorReplying(t, plan)} />,
      })),
      ...(draft && draftAt?.at === at ? [{
        key: "draft", anchor: draftAt.anchor,
        node: <DraftCard key={`${draft.kind}:${draft.where}`} draft={draft} number={Math.max(0, ...threads.map((t) => t.id)) + 1} stale={draftStale} busy={busy}
          text={drafting?.text ?? ""} onText={setDraftReason} onSend={(text) => void sendDraft(text)} onCancel={() => setDrafting(null)} change={draftChange}
          className="grid gap-1.5 border-l border-branch pb-0.5 pl-[13px] pt-px font-sans text-[12.5px] leading-normal text-ink-2" />,
      }] : []),
    ];
    const noted = openThreads.length > 0 || !!draft;
    const body = (s: PlanSectionDTO) => (s.kind === "mermaid" || s.kind === "svg" ? <Diagram section={s} figure={figures.get(s.id) ?? 1} /> : textBody(s.id, s.title));
    const cards = (at: string) => (placed.get(at) ?? []).map((d) => (
      <Annotated key={d.id} notes={notesFor(`decision:${d.id}`)}>
        <DecisionCard decision={d} n={ordered.indexOf(d) + 1} of={ordered.length} open={open} canPick={canPick} disabled={busy} picked={chosen(d)} approver={approver} nameOf={(who) => who === "person" ? "You" : nameOf(who)}
          onPick={(o) => void pickOption(d.id, o)} onComment={canComment ? () => comment({ kind: "decision", decision: d.id }, decisionName(d.id)) : undefined}
          answers={canPick ? ownAnswers(d.id) : undefined} />
      </Annotated>
    ));
    const titleAside = (s: PlanSectionDTO) => {
      const here = editing?.section === s.id;
      return (here || mode === "editing") && <span className={`${aside} ml-3`}>{here ? (live ? "You are writing here · live" : "You are editing") : s.kind !== "mermaid" && s.kind !== "svg" && editControl(s.id, s.title)}</span>;
    };
    const stillOpen = [openThreads.length - suggestions > 0 && plural(openThreads.length - suggestions, "comment"), suggestions > 0 && plural(suggestions, "suggestion")].filter(Boolean);
    const lineFor = plan.state === "open" ? "Nothing is built until you approve" : plan.state === "approved" ? "brindle is building it" : statusLine;
    const stageTone = plan.state === "open" ? "text-sap" : plan.state === "approved" ? "text-pass" : "text-ink-3";
    // The bottom bar while decisions are open: how many, and the next one.
    const nextUp = left.length > 0 && (
      <>
        <span className="flex items-center gap-2 text-[13.5px] font-semibold text-ink"><span className={`${pill} !text-[13.5px] !font-semibold text-sap`}>{`${left.length} left to decide`}</span></span>
        <span className="text-xs text-ink-3">
          {"Next: "}
          <a href={`#plan-decision-${left[0].id}`} data-testid="plan-next-decision" className="text-ink-2 no-underline hover:text-ink hover:underline" onClick={(e) => { e.preventDefault(); jump(left[0].id); }}>{`${left[0].title} ↓`}</a>
        </span>
      </>
    );
    // In team review the status line says what sign-off waits for.
    const reviewed = !!gate || !!teammate;
    const status = reviewed ? (
      <div className="mt-3.5 grid gap-1.5">
        <DocStatus stage={stage} presentation={closed ? "closed" : undefined} tone={frozen ? "branch" : plan.state === "approved" || gate?.state === "ready" ? "pass" : "sap"} line={statusLine}
          byline={byline} workItem={workItem} state={plan.state} held={policyHeld ? "policy" : undefined} />
        {gateNotes}
      </div>
    ) : (
      <div className="mt-3.5 flex flex-wrap items-center gap-x-4 gap-y-1.5 font-sans text-[12.5px] leading-[1.4] text-ink-2" data-testid="plan-status" data-state={plan.state} data-presentation={closed ? "closed" : undefined}>
        <span className={`${pill} text-xs ${stageTone}`}>{stage}</span>
        <span>{lineFor}</span>
        {plan.reference && <span className="text-ink-3" data-testid="doc-reference">{plan.reference}</span>}
        <span className="text-ink-3">{byline}</span>
      </div>
    );
    // The sign-off sheet under the outline: once a version is frozen (or for a doc reviewed without live text).
    const signOffs = gate && (frozen || !useLiveReview) && (
      <SignOffSheet version={frozen ? `version ${frozen.id}` : `rev ${plan.rev}`} rows={sheetRows} since={sinceRevs && lastRead !== null ? { from: lastRead, changes: sinceRevs } : null}
        onShowChanges={source.history ? () => setHistoryOpen(true) : undefined} />
    );
    const teammateBar = teammate && (
      <>
        <div role="status" className="grid gap-px" data-testid="plan-next" data-state={left.length ? "open" : teammate.signedOff ? "signed" : "ready"} data-left={left.length}>
          {(teammate.rights.signOff || teammate.signedOff || left.length > 0) && <span className="text-xs text-ink-3">{accessLine}</span>}
          {left.length ? nextUp : (
            <>
              <span className="flex items-center gap-2 text-[13.5px] font-semibold text-ink">
                {teammate.signedOff ? <Tick className="!text-[13.5px] !font-semibold">{frozen ? `You signed off version ${frozen.id}` : `You signed off r${plan.rev}`}</Tick>
                  : teammate.rights.signOff ? (frozen ? signName : "Ready for your sign-off") : accessLine}
              </span>
              {summary && <span className="text-xs text-ink-3">{summary}</span>}
            </>
          )}
        </div>
        {teammate.rights.signOff && !teammate.signedOff && (
          <LoadingButton className={main} busy={sending === "approve"} disabled={!!historical || busy || reviewBlocked || left.length > 0} data-testid="plan-sign-off"
            title={left.length ? "Pick an option in every decision first" : undefined} data-again="signoff" onClick={() => void signOff()}>{signName}</LoadingButton>
        )}
      </>
    );
    const versionMenu = source.history ? (
      <Menu label={<span data-testid="plan-rev">{`${versionLabel} · History`}</span>} buttonClass={`${quiet} px-[9px] text-xs !text-ink-3`} title="See earlier revisions and what changed"
        place="down" panelClass="w-[380px] max-w-[86vw] max-h-[60vh] overflow-auto" open={historyOpen} onOpenChange={setHistoryOpen} keepMounted testid="plan-history-menu">
        <DocumentHistory client={source.history} planId={plan.id} refresh={historyVersion} enabled={historyOpen} nameOf={nameOf}
          onCompare={(comparison, close) => { setHistorical(comparison ? { comparison, close } : null); if (comparison) setHistoryOpen(false); }} />
      </Menu>
    ) : <span className="whitespace-nowrap px-[9px] text-xs text-ink-3" data-testid="plan-rev">{versionLabel}</span>;
    // A shared plan builds only once every required reviewer has signed.
    const gateHolds = !!gate && gate.state !== "ready";
    const approveBlocked = !!historical || busy || left.length > 0 || reviewBlocked || uploading > 0 || gateHolds;
    return (
      <>
        {chrome && (
          <ReaderBar lead={chrome.lead} version={versionMenu} presence={presence}
            modes={{ current: mode, picker: <ModeMenu modes={chrome.modes} current={mode} allowed={(m) => (locked ? m === "viewing" : chrome.allowed(m))} onPick={chrome.onMode} /> }}
            share={!historical && shareMenu} />
        )}
        {!chrome && presence && <div className="hidden">{presence}</div>}
        {teammate && top}
        <div ref={setStripHost} className="contents" />
        {typing && !historical && <FormatBar />}
        <div className="flex min-h-0 flex-1 bg-canvas" data-testid="plan-design-doc">
          <Outline parts={parts} placed={placed} isOpen={waiting} decided={decided} total={ordered.length} scrollPane={doc} footer={signOffs || undefined} />
          <div ref={doc} className="relative min-h-0 min-w-0 flex-1 overflow-auto border-l border-line max-[900px]:border-l-0" data-testid="plan-doc" data-mode={historical ? "viewing" : mode}
            onMouseUp={historical ? undefined : pick} onKeyUp={historical ? undefined : pick} onClick={historical ? undefined : followAnchor}>
            {historical ? <DocHistoryComparison key={historical.comparison.revision.rev} comparison={historical.comparison} onClose={historical.close} /> : (
              <article className={`pb-[84px] pl-[68px] pt-12 max-[900px]:pb-16 max-[900px]:pl-[46px] max-[900px]:!pr-[22px] max-[900px]:pt-8 ${noted ? "pr-6 max-[1180px]:pr-[68px]" : "pr-[68px]"}`}>
                <div className={`font-serif text-[15.5px] leading-[1.68] text-ink-2 ${noted ? "w-[min(660px,100%_-_292px)] max-[1180px]:mx-auto max-[1180px]:w-[min(660px,100%)]" : "mx-auto w-[min(660px,100%)]"}`}>
                  <h1 className="m-0 min-w-0 [overflow-wrap:anywhere] font-serif text-[31px] font-semibold leading-[1.16] tracking-[-0.012em] text-ink [text-wrap:balance]">{plan.title}</h1>
                  {status}
                  {plan.team_ended && <p className="m-0 mt-2 font-sans text-[12.5px] text-ink-2" data-testid="plan-team-ended">Your Team plan ended, so this is now a personal plan.</p>}
                  {textPlan.lede && (
                    <Annotated notes={notesFor(LEDE)} content={bodyOf(LEDE)} className="mt-[26px]">
                      <div id="plan-lede" className="grid gap-2.5 text-[18.5px] leading-[1.55] text-ink">
                        {mode === "editing" && editing?.section !== LEDE && <span className={`${aside} justify-self-end`}>{editControl(LEDE, "the overview")}</span>}
                        {textBody(LEDE, "the overview")}
                      </div>
                    </Annotated>
                  )}
                  {!textPlan.lede && notesFor(LEDE).length > 0 && <Annotated notes={notesFor(LEDE)} className="mt-[26px]"><span /></Annotated>}
                  {parts.map((p) => (
                    <section key={p.section.id} id={`plan-${p.section.id}`} data-spine={p.section.id} aria-labelledby={`plan-title-${p.section.id}`} className="mt-[46px] scroll-mt-14 border-t border-line pt-7">
                      <h2 className="relative m-0 mb-3.5 flex items-baseline justify-between font-serif text-[21.5px] font-semibold leading-[1.25] tracking-[-0.005em] text-ink">
                        <span aria-hidden className="absolute -left-9 top-[0.5em] font-mono text-[11.5px] font-normal leading-none text-ink-3 max-[900px]:-left-[26px]">{p.n}</span>
                        <span id={`plan-title-${p.section.id}`}>{p.section.title}</span>
                        {titleAside(p.section)}
                      </h2>
                      <Annotated notes={notesFor(p.section.id)} content={p.section.markdown}>{body(p.section)}</Annotated>
                      {cards(p.section.id)}
                      {p.subs.map((s) => (
                        <div key={s.id} id={`plan-${s.id}`} role="region" aria-labelledby={`plan-title-${s.id}`}>
                          <h3 className="m-0 mb-2 mt-7 flex items-baseline justify-between font-serif text-base font-semibold leading-[1.3] text-ink">
                            <span id={`plan-title-${s.id}`}>{s.title}</span>
                            {titleAside(s)}
                          </h3>
                          <Annotated notes={notesFor(s.id)} content={s.markdown}>{body(s)}</Annotated>
                          {cards(s.id)}
                        </div>
                      ))}
                    </section>
                  ))}
                  {cards("")}
                </div>
              </article>
            )}
            {highlightBar}
          </div>
          {sharePanel}
        </div>
        <footer className="flex min-h-[60px] flex-wrap items-center justify-between gap-x-5 gap-y-3 border-t border-line-strong bg-canvas-2 px-4 py-2.5 font-sans" data-testid="plan-verdict" ref={verdict}>
          {draft?.kind === "suggest" ? suggestingBar : live && (editing || raw) ? editingBar : teammate ? teammateBar : open ? (
            <>
              <div role="status" className="grid gap-px" data-testid="plan-next" data-state={left.length ? "open" : "ready"} data-left={left.length}>
                {left.length ? nextUp : (
                  <>
                    <span className="flex items-center gap-2 text-[13.5px] font-semibold text-ink"><Tick className="!text-[13.5px] !font-semibold">Everything is decided</Tick></span>
                    <span className="text-xs text-ink-3">{gateHolds ? summary || gateWhy : `Approve starts the build.${stillOpen.length ? ` ${stillOpen.join(" and ")} ${openThreads.length === 1 ? "is" : "are"} still open.` : ""}`}</span>
                  </>
                )}
              </div>
              <div className="flex items-center gap-2">
                {client?.cancelPlan && (
                  <Menu label="Cancel planning" buttonClass={quiet} place="up" open={cancelOpen} onOpenChange={setCancelOpen} testid="plan-cancel-menu" again="cancel">
                    <h4 className={menuTitle}>Close this plan without approving?</h4>
                    <p className="m-0">Nothing is built. The tasks waiting on this plan go back to your supervisor, who asks you what to do with each.</p>
                    <div className={menuRow}>
                      <button type="button" className={quiet} onClick={() => setCancelOpen(false)}>Keep planning</button>
                      <LoadingButton className={danger} busy={sending === "cancel"} disabled={!!historical || busy || uploading > 0} data-testid="plan-cancel" onClick={() => void cancelPlanning()}>Cancel planning</LoadingButton>
                    </div>
                  </Menu>
                )}
                <VerdictNote hasFeedback={hasFeedback}>{(note, setNote, canRequest) => (
                <Menu label="Request changes" buttonClass={button} place="up" open={changesOpen} onOpenChange={setChangesOpen} testid="plan-changes-menu" again="changes">
                  <h4 className={menuTitle}>What should change?</h4>
                  <textarea className={menuNote} aria-label="Note for the supervisor" placeholder="A note for the supervisor (optional if you have commented)" value={note} onChange={(e) => setNote(e.target.value)} />
                  {(attached.length > 0 || uploading > 0) && (
                    <div className="flex flex-wrap items-center gap-1.5">
                      {attached.map((path, i) => (
                        <span key={`${path}:${i}`} className="inline-flex items-center gap-1 whitespace-nowrap rounded-sm border border-line px-1.5 py-px font-mono text-[11px] text-ink-2">
                          {fileName(path)}
                          <button type="button" aria-label={`Remove ${fileName(path)}`} className="cursor-pointer text-ink-3 hover:text-ink" onClick={() => setAttached((list) => list.filter((_, j) => j !== i))}>×</button>
                        </span>
                      ))}
                      {uploading > 0 && <span className="text-xs text-ink-3">Attaching…</span>}
                    </div>
                  )}
                  <p className="m-0">Your comments, suggestions and picks go back with it. The supervisor writes a new revision and you are asked again.</p>
                  <div className={menuRow}>
                    {canAttach ? (
                      <>
                        <input ref={picker} type="file" multiple hidden data-testid="plan-attach-input" onChange={(e) => { const files = Array.from(e.target.files ?? []); e.target.value = ""; if (files.length) void upload(files); }} />
                        <button type="button" className={quiet} onClick={() => picker.current?.click()}>Attach a file</button>
                      </>
                    ) : <span />}
                    <LoadingButton className={button} busy={sending === "changes"} disabled={!!historical || busy || !canRequest || uploading > 0}
                      title={canRequest ? undefined : "Comment, add a note or pick differently first"} data-testid="plan-send-back" onClick={() => void decide(false)}>
                      Send back
                    </LoadingButton>
                  </div>
                </Menu>
                )}</VerdictNote>
                <LoadingButton className={main} busy={sending === "approve"} disabled={approveBlocked} data-testid="plan-build" data-gate={gate?.state}
                  title={left.length ? "Pick an option in every decision first" : gateHolds ? gateWhy : "Approve this plan and start building it"} data-again="approve" onClick={() => void decide(true)}>
                  Approve
                </LoadingButton>
              </div>
            </>
          ) : plan.state === "approved" ? (
            <>
              <div role="status" className="grid gap-px" data-testid="plan-next" data-state="approved">
                <span className="flex items-center gap-2 text-[13.5px] font-semibold text-ink"><Tick className="!text-[13.5px] !font-semibold">Approved</Tick></span>
                <span className="text-xs text-ink-3">brindle is splitting the work and starting it now.</span>
              </div>
              {chrome?.onBack && <button type="button" className={button} onClick={chrome.onBack}>Back to the session</button>}
            </>
          ) : <div className="grid w-full gap-2">{decidedBar}</div>}
          {footerNotes("basis-full")}
          {error && !net && <p role="alert" className="m-0 basis-full text-xs text-sap">{error}</p>}
        </footer>
      </>
    );
  }
  if (designDoc) return planningDoc();

  return (
    <>
      {chrome && (
        <ReaderBar lead={chrome.lead} badges={chrome.badges} actions={chrome.actions}
          version={<span className="whitespace-nowrap text-xs text-ink-3" title={`${plan.id} · rendered by brindle from the plan's own text${frozen ? ` · rev ${plan.rev}` : ""}`} data-testid="plan-rev">{versionLabel}</span>}
          presence={presence}
          markdown={{ pressed: raw, onToggle: () => setRaw((r) => !r) }}
          modes={{ current: mode, picker: <ModePicker modes={chrome.modes} current={mode} allowed={(m) => (locked ? m === "viewing" : chrome.allowed(m))} onPick={chrome.onMode} /> }}
          share={!historical && shareMenu} />
      )}
      {!chrome && presence && <div className="hidden">{presence}</div>}
      {top}
      <div ref={setStripHost} className="contents" />
      {typing && !historical && <FormatBar hint={raw ? "The buttons write the Markdown for you: Bold wraps the selection in **." : undefined} />}
      <div className="flex min-h-0 flex-1">
        <div ref={doc} className="relative min-h-0 min-w-0 flex-1 overflow-auto" data-testid="plan-doc" data-mode={historical ? "viewing" : mode} onMouseUp={historical ? undefined : pick} onKeyUp={historical ? undefined : pick} onClick={historical ? undefined : followAnchor}>
          {historical ? <DocHistoryComparison key={historical.comparison.revision.rev} comparison={historical.comparison} onClose={historical.close} /> : <article className="flex flex-col gap-5 pb-10 pl-14 pr-12 pt-7 font-serif text-[15px] leading-[1.65] text-ink-2">
            <div className="grid gap-3 font-sans">
              <h1 className="m-0 min-w-0 [overflow-wrap:anywhere] font-serif text-[28px] font-semibold leading-[1.2] tracking-[-0.01em] text-ink">
                {plan.title} <DocReference reference={plan.reference} className="ml-1 font-sans" />
              </h1>
              <DocStatus stage={stage} presentation={closed ? "closed" : undefined} tone={frozen ? "branch" : plan.state === "approved" || gate?.state === "ready" ? "pass" : "sap"} line={statusLine}
                byline={byline} workItem={workItem}
                brief={!!plan.ask} briefOpen={briefOpen} onBrief={() => setBriefOpen((o) => !o)}
                state={plan.state} held={policyHeld ? "policy" : undefined} />
              {plan.team_ended && <p className="m-0 text-[12.5px] text-ink-2" data-testid="plan-team-ended">Your Team plan ended, so this is now a personal plan.</p>}
              {gateNotes}
              {plan.ask && briefOpen && (
                <p id="plan-ask" className="m-0 flex flex-wrap items-baseline gap-x-2 border-l-2 border-line-strong pl-3 text-[12.5px] text-ink-2" data-testid="plan-ask">
                  <span className="text-ink-3">What the supervisor understood:</span>
                  <span className="max-w-[78ch]">{plan.ask}</span>
                  {commentButton(() => comment({ kind: "section", section: "ask" }, partName("ask")), "Correct it")}
                </p>
              )}
            </div>
            {raw ? (
              <DocMarkdown plan={textPlan} state={markdownState} version={frozen ? `version ${frozen.id}` : undefined}
                editorFor={(s, title) => (live ? liveEditor(s, title, "source") : editing?.section === s ? editBox(title) : null)} />
            ) : <>
            {textPlan.lede && (
              <div id="plan-lede" className="grid gap-2.5">
                {mode === "editing" && editing?.section !== LEDE && <span className={`${aside} justify-self-end`}>{editControl(LEDE, "the overview")}</span>}
                {textBody(LEDE, "the overview")}
              </div>
            )}
            {plan.sections.map(section)}
            {plan.decisions.map((d, i) => {
              const mine = settled(d) ? (open ? "confirmed" : null) : !canPick ? "not decided yet" : d === next ? "yours to take next" : null;
              return (
                <section key={d.id} id={`plan-decision-${d.id}`} aria-labelledby={`plan-title-decision-${d.id}`} data-next={d === next || undefined} data-state={open && !settled(d) ? "undecided" : undefined} className={`grid gap-2.5 border-t pt-4 ${open && !settled(d) ? "border-pass" : "border-line"}`}>
                  <h3 className={heading}>
                    <span aria-hidden className={margin}>{plan.sections.length + i + 1}</span>
                    <span id={`plan-title-decision-${d.id}`}>{`Decision · ${d.title}`}</span>
                    <span className={aside}>{`decision ${i + 1} of ${plan.decisions.length}${mine ? ` · ${mine}` : ""}`}</span>
                  </h3>
                  <Decision decision={d} chosen={chosen(d) ?? (canPick ? d.mine ?? null : null)} open={canPick && !busy} onPick={(o) => void pickOption(d.id, o)} nameOf={nameOf} />
                  <p className="flex max-w-[70ch] flex-wrap items-baseline gap-x-2 font-sans text-[12.5px] leading-normal text-ink-3">
                    {d.challenge && <span>{`${d.mine ? "Against its own pick: " : ""}${d.challenge}`}</span>}
                    {commentButton(() => comment({ kind: "decision", decision: d.id }, decisionName(d.id)))}
                  </p>
                </section>
              );
            })}
            </>}
          </article>}
          {highlightBar}
        </div>
        {sharePanel}
        {!sharingOpen && (columnOpen ? (
          <Review plan={plan} tab={draft ? "comments" : tab} onTab={setTab} todo={todo} decided={decided} onJump={jump} where={where} draft={draft} draftStale={draftStale} busy={busy}
            draftText={drafting?.text ?? ""} onDraftText={setDraftReason}
            onDraftSend={(text) => void sendDraft(text)} onDraftCancel={() => setDrafting(null)} draftChange={draftChange}
            actions={actions} limits={teammate ? { reply: canComment, resolve: (t) => canComment && t.author === "person" } : undefined} onShow={showThread} shared={sharedFiles} onCollapse={() => { setDrafting(null); collapse(true); }}
            active={followed ? Number(followed.thread) : null} judge={teammate ? authorName : undefined} sheet={sheet}
            historyPanel={source.history && <DocumentHistory client={source.history} planId={plan.id} refresh={historyVersion} nameOf={nameOf}
              onCompare={(comparison, close) => setHistorical(comparison ? { comparison, close } : null)} />}
            history={history} historyRev={historyRev} onHistoryPick={setHistoryRev} workItems={workItems} work={work} canLink={!teammate || canComment} />
        ) : (
          <ReviewStrip comments={openThreads.length} decisions={plan.decisions.length - decided} onOpen={() => collapse(false)} />
        ))}
      </div>
      <footer className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-2.5 border-t border-line-strong bg-canvas-2 px-s2 py-3" data-testid="plan-verdict" ref={verdict}>
        {draft?.kind === "suggest" ? suggestingBar : live && (editing || raw) ? editingBar : teammate ? (
          <>
            <div className="grid min-w-0 gap-0.5" data-testid="plan-next">
              {(teammate.rights.signOff || teammate.signedOff || next) && <span className="text-xs text-ink-3">{accessLine}</span>}
              <span className="flex items-center gap-2.5 text-sm font-semibold text-ink">
                <span className={`${label} text-[11px]`}>Next</span>
                <span className="truncate">{next ? next.title : teammate.signedOff ? (frozen ? `You signed off version ${frozen.id}` : `You signed off r${plan.rev}`) : teammate.rights.signOff ? (frozen ? signName : "Sign off this revision") : accessLine}</span>
              </span>
              <span className="text-xs text-ink-3">{summary}</span>
            </div>
            <div className="flex items-center gap-2">
              {next && <button type="button" className={quiet} onClick={() => jump(next.id)}>Jump to it</button>}
              {!next && teammate.rights.signOff && !teammate.signedOff && (
                <LoadingButton className={main} busy={sending === "approve"} disabled={!!historical || busy || reviewBlocked} data-testid="plan-sign-off" data-again="signoff" onClick={() => void signOff()}>{signName}</LoadingButton>
              )}
            </div>
          </>
        ) : open ? (
          <VerdictNote hasFeedback={hasFeedback}>{(note, setNote, canRequest) => (
          <>
            <div className="grid min-w-0 gap-0.5" data-testid="plan-next">
              <span className="flex items-center gap-2.5 text-sm font-semibold text-ink">
                <span className={`${label} text-[11px]`}>Next</span>
                <span className="truncate">{next ? next.title : gate ? (gate.state === "ready" ? "Ready to build" : gateWhy) : "Sign off this plan"}</span>
              </span>
              <span className="text-xs text-ink-3">
                {summary}
                {sharedFiles.size > 0 && <span className="text-sap">{`${summary ? " " : ""}${plural(sharedFiles.size, "file is", "files are")} in more than one worker.`}</span>}
              </span>
            </div>
            <div className="flex items-center gap-2">
              {next && <button type="button" className={quiet} onClick={() => jump(next.id)}>Choose a decision</button>}
              {client?.cancelPlan && <LoadingButton className={quiet} busy={sending === "cancel"} disabled={!!historical || busy || uploading > 0} data-testid="plan-cancel" data-again="cancel" onClick={() => void cancelPlanning()}>Cancel planning</LoadingButton>}
              <LoadingButton className={button} busy={sending === "changes"} disabled={!!historical || busy || !canRequest || uploading > 0}
                title={canRequest ? "Send it back to the supervisor for a new revision, with your notes" : "Comment, add a note or pick differently first"} data-again="changes" onClick={() => void decide(false)}>
                Request changes
              </LoadingButton>
              {/* The person's selections and verdict travel together. */}
              {(
                <LoadingButton className={main} busy={sending === "approve"} disabled={!!historical || busy || !!next || reviewBlocked || uploading > 0 || (!!gate && gate.state !== "ready")} title={next ? "Choose every decision first" : gate && gate.state !== "ready" ? gateWhy : undefined}
                  data-again="approve" onClick={() => { if (next) jump(next.id); else void decide(true); }}
                  data-testid="plan-build" data-gate={gate?.state}>
                  Approve
                </LoadingButton>
              )}
            </div>
            <div className="col-span-full flex items-center gap-2.5 border-t border-line pt-2.5">
              <input className={`${field} min-w-40 flex-1 text-[13px]`} aria-label="Note for the supervisor" placeholder="Note for the supervisor…" value={note} onChange={(e) => setNote(e.target.value)} />
              {attached.map((path, i) => (
                <span key={`${path}:${i}`} className="inline-flex items-center gap-1 whitespace-nowrap rounded-sm border border-line px-1.5 py-px font-mono text-[11px] text-ink-2">
                  {fileName(path)}
                  <button type="button" aria-label={`Remove ${fileName(path)}`} className="cursor-pointer text-ink-3 hover:text-ink" onClick={() => setAttached((list) => list.filter((_, j) => j !== i))}>×</button>
                </span>
              ))}
              {uploading > 0 && <span className="text-xs text-ink-3">Attaching…</span>}
              {canAttach && (
                <>
                  <input ref={picker} type="file" multiple hidden data-testid="plan-attach-input" onChange={(e) => { const files = Array.from(e.target.files ?? []); e.target.value = ""; if (files.length) void upload(files); }} />
                  <button type="button" className={`${quiet} px-1`} aria-label="Attach a file" title="Attach a file" onClick={() => picker.current?.click()}>
                    <PaperclipIcon size={14} />
                  </button>
                </>
              )}
              <span className="max-w-[44ch] text-right text-[11.5px] text-ink-3">"Request changes" sends it back to the supervisor for a new revision, with your notes.</span>
            </div>
          </>
          )}</VerdictNote>
        ) : decidedBar}
        {footerNotes("col-span-full")}
        {error && !net && error !== gate?.unconfirmed?.message && <p role="alert" className="col-span-full text-xs text-sap">{error}</p>}
      </footer>
    </>
  );
}
