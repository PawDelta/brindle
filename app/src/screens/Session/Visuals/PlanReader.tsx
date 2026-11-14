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
import { DaemonError, type DaemonClient, type DocScope, type LibraryPerson, type LiveDocState, type Plan, type PlanAnchor, type PlanChange, type VisualDTO } from "../../../api/client";
import { liveReviewGate } from "../../../api/liveReview";
import type { PlanDecisionDTO, PlanSectionDTO, PlanThreadDTO } from "../../../api/schema";
import { LoadingButton } from "../../../components/LoadingButton";
import { ago } from "../../../components/ProviderConnect";
import { PaperclipIcon } from "../../../components/icons";
import { button, field, label, main, quiet } from "../../../components/ui";
import { CloudLoginStatus, useCloudLogin } from "../../Inbox/CloudSignIn";
import type { ExportClient } from "../../Knowledge/ExportMenu";
import type { PlanDocClient, PlanSharingClient, PlansClient, VisualsClient } from "./client";
import { BackButton, KeepButton, fileAttachment, fileName, message, useEscBack } from "./chrome";
import { Popover, ReaderBar } from "./DocBar";
import { Diagram } from "./DocDiagram";
import { FormatBar, FormatProvider, textareaTarget, useFormatTarget } from "./DocFormat";
import type { HistoryEvent, WorkItem } from "./DocHistory";
import { DocMarkdown } from "./DocMarkdown";
import { Review, ReviewStrip, SignOffSheet, type Draft, type ReviewTab, type SignOffRow, type ThreadActions, type TodoItem } from "./DocReview";
import { Reviewers } from "./DocReviewers";
import type { LiveClient } from "./DocEditor";
import { DocPresence, type LiveCapability } from "./DocPresence";
import { DocStatus, type StatusReviewer } from "./DocStatus";
import { DocText, changeBetween, selectedRange, toPoints, toUnits, type Mark } from "./DocText";
import { ExportButton } from "./ExportButton";
import { Decision, Tasks, overlaps, passChip, personName } from "./PlanCards";
import { planSource, type DocSource } from "./docSource";
import { StoreContext } from "../../../store/useStore";

type Client = VisualsClient & PlansClient & PlanDocClient & Partial<ExportClient> & Partial<Pick<DaemonClient, "attach">> & Partial<PlanSharingClient> & Partial<LiveClient>;

/* How far along a plan is: its revision, then decided or not. Revisions only grow and a decided
   revision never reopens, so a response with less progress than the one on screen is stale. */
/* The live editor brings CodeMirror and Loro's WebAssembly: loaded when a section is first edited live. */
const DocEditor = lazy(() => import("./DocEditor").then((m) => ({ default: m.DocEditor })));

const progress = (p: Plan) => p.rev * 2 + (p.state === "open" ? 0 : 1);
const noSubscribe = () => () => {};

/* The doc's writes, kept in order by the reader: `begin` is refused while another is out, and
   `end` hands back the engine's reply (the plan as it is now), or null when the write failed. */
export interface Writes {
  begin(): boolean;
  end(plan: Plan | null): void;
}

export type Mode = "viewing" | "suggesting" | "editing";
export const MODES: { id: Mode; name: string; hint: string }[] = [
  { id: "viewing", name: "Viewing", hint: "Read and comment" },
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
}

/* The doc from its source, kept current, with its writes kept in order. `version` changing asks
   for one more read. */
export function useDoc<P extends Plan>(source: DocSource<P>, version: unknown) {
  const [doc, setDoc] = useState<P | null>(null);
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
    end: (p) => {
      if (p && write.current.access === access.current) place(p as P, write.current.passed);
      write.current = { out: false, passed: false, access: access.current };
      seq.current++;
      setAgain((n) => n + 1);
    },
  }), [place]);
  useEffect(() => {
    const mine = ++seq.current;
    source.read().then(
      (p) => {
        if (mine !== seq.current) return;
        if (place(p) && write.current.out) write.current.passed = true;
        setError(null);
      },
      (e) => {
        if (mine !== seq.current) return;
        setError(message(e));
        if (source.lost?.(e)) {
          access.current++;
          latest.current = null;
          setDoc(null);
        }
      },
    );
  }, [source, place, version, again]);
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

export function PlanReader({ client, visual, repo, onBack, asking, onChanged }: {
  client: Client;
  visual: VisualDTO;
  repo?: string;
  onBack(): void;
  asking?: boolean;
  /* The engine's row after a Keep, ahead of the bus. */
  onChanged(visual: VisualDTO): void;
}) {
  useEscBack(onBack);
  const [exporting, setExporting] = useState(false);
  const [mode, setMode] = useState<Mode>("viewing");
  const store = useContext(StoreContext);
  const updates = useSyncExternalStore(store?.subscribe ?? noSubscribe, () => {
    const s = store?.getState();
    return `${s?.recent.filter((e) => e.root_id === visual.session || e.payload.caller_id === visual.session).at(-1)?.id ?? 0}:${s?.snapshotVersion ?? 0}`;
  });
  // Read again whenever the session's row says the plan changed (a revision, a verdict).
  const changed = `${visual.id}:${visual.rev}:${visual.answer?.at ?? ""}`;
  const source = useMemo(() => planSource(client, visual.id), [client, visual.id]);
  const { doc: shown, error, writes } = useDoc(source, `${changed}|${updates}`);
  const exportClient = client.exportVisual && client.exportDoc && client.docPolicy && client.docExportConfig ? (client as ExportClient) : undefined;
  // A decided plan is read, not edited; the engine takes a suggestion until it is approved.
  const allowed = (m: Mode) => m === "viewing" || (!!shown && (m === "editing" ? shown.state === "open" : shown.state !== "approved"));
  const current = allowed(mode) ? mode : "viewing";
  const lead = (
    <>
      <BackButton onBack={onBack} asking={asking} />
      <span className="mx-1 h-5 w-px bg-line" aria-hidden />
      <h2 className="min-w-0 truncate text-[13px] font-semibold">{visual.title}</h2>
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
        <DesignDoc key={shown.id} source={source} author={{ client, visual }} plan={shown} mode={current} writes={writes}
          chrome={{ lead, badges, actions, modes: MODES, allowed, onMode: setMode }}
          top={shown.state === "approved" && (
            <section aria-label="Plan board" className="flex max-h-[50vh] shrink-0 flex-col gap-3.5 overflow-auto border-b border-line px-s3 py-5">
              <Tasks plan={shown} shared={overlaps(shown)} />
            </section>
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
/* "A · Your own server" is confirmed as "Confirm A"; a label with no such lead, by its name. */
const shortName = (optionLabel: string) => /^([A-Za-z0-9]{1,3})\s*[·:.)-]\s/.exec(optionLabel)?.[1] ?? optionLabel;
const heading = "relative m-0 flex items-baseline justify-between gap-3 font-serif text-[17px] font-semibold leading-[1.3] text-ink";
const margin = "absolute -left-7 top-[0.3em] font-mono text-xs font-normal text-ink-3";
const aside = "inline-flex items-center gap-1.5 whitespace-nowrap font-sans text-xs font-normal text-ink-3";
const show = (el: Element | null | undefined, block: ScrollLogicalPosition = "start") => el?.scrollIntoView?.({ block, behavior: "smooth" });

/* What a highlight offers, where it was made: a range of a section's text, or a diagram. */
type Picked = { x: number; y: number } & ({ kind: "text"; section: string; start: number; quote: string } | { kind: "diagram"; section: string });

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
  signedOff: boolean;
  author: string | null;
  directory: LibraryPerson[];
}

export function DesignDoc({ source, author, teammate: receivedTeammate, plan: receivedPlan, mode: chosenMode, writes, liveClient, chrome, top, history = null, workItems = null, workItem = null }: {
  source: DocSource;
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
  return (
    <FormatProvider>
      <DesignDocBody source={source} author={author} teammate={receivedTeammate} plan={receivedPlan} mode={chosenMode} writes={writes} liveClient={liveClient} chrome={chrome} top={top} history={history} workItems={workItems} workItem={workItem} />
    </FormatProvider>
  );
}

function DesignDocBody({ source, author, teammate: receivedTeammate, plan: receivedPlan, mode: chosenMode, writes, liveClient, chrome, top, history, workItems, workItem }: {
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
  const docLive = liveClient ?? (receivedPlan.gate && client?.visualLive ? (client as LiveClient) : null);
  const [liveState, setLiveState] = useState<LiveDocState | null>(null);
  const [liveCapability, setLiveCapability] = useState<LiveCapability>("unknown");
  const useLiveReview = !!docLive && liveCapability !== "unsupported";
  // A version frozen for sign-off can't change in any view: the reader only reads (and comments).
  const locked = useLiveReview && liveState?.lock === "sign_off";
  const mode: Mode = locked ? "viewing" : chosenMode;
  const frozen = useLiveReview && liveState?.lock === "sign_off" ? liveState.version : null;
  const [raw, setRaw] = useState(false);
  const [sharingOpen, setSharingOpen] = useState(false);
  const [briefOpen, setBriefOpen] = useState(false);
  const [stripHost, setStripHost] = useState<HTMLDivElement | null>(null);
  const [draftAfter, setDraftAfter] = useState<string | null>(null);
  const [historyRev, setHistoryRev] = useState<number | null>(null);
  const lastRead = useLastRead(receivedPlan.id, receivedPlan.rev);
  const plan = useLiveReview ? { ...receivedPlan, gate: liveReviewGate(receivedPlan.gate, liveState) } : receivedPlan;
  const teammate = receivedTeammate && useLiveReview ? { ...receivedTeammate, signedOff: !!receivedTeammate.me && !!liveState?.version?.signOffs.includes(receivedTeammate.me) } : receivedTeammate;
  const reviewVersion = useLiveReview ? liveState?.version?.id : undefined;
  const reviewBlocked = useLiveReview && reviewVersion === undefined;
  const visual = author?.visual;
  const canComment = !teammate || teammate.rights.comment;
  const canSuggest = !teammate || teammate.rights.suggest;
  // Picks are the person's to make: a teammate's is a vote, which the Library ties to commenting.
  const open = plan.state === "open" && canComment;
  const threads = useMemo(() => plan.threads ?? [], [plan.threads]);
  const sharedFiles = useMemo(() => overlaps(plan), [plan]);
  const [picks, setPicks] = useState<Record<string, string>>({});
  const [note, setNote] = useState("");
  const [attached, setAttached] = useState<string[]>([]);
  const [uploading, setUploading] = useState(0);
  const [sending, setSending] = useState<"confirm" | "approve" | "changes" | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<Picked | null>(null);
  // What the person is writing belongs to the text it was started on (`source`): the doc stays
  // open across revisions, and a draft begun on one must not be sent against another's text.
  const [drafting, setDrafting] = useState<{ draft: Draft; source: string } | null>(null);
  const draft = drafting?.draft ?? null;
  const [box, setEditing] = useState<{ section: string; text: string; source: string } | null>(null);
  // A section's box belongs to Suggesting and Editing: back in Viewing (or decided), it is closed.
  const editing = mode === "viewing" ? null : box;
  const [followed, setFollowed] = useState<{ thread: string } | null>(null);
  const [tab, setTab] = useState<ReviewTab>("comments");
  const [collapsed, setCollapsed] = useState(storedCollapsed);
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
  // Opening a doc in review brings back what teammates wrote; what changed arrives as an event.
  // Failing says nothing new: the gate shows what was last heard, and an approval asks again.
  useEffect(() => {
    if (shared && plan.state !== "approved") sharing?.syncPlan(plan.id).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per doc, when it is known to be shared
  }, [plan.id, shared]);
  const nameOf = (sub: string) => directory.find((p) => p.sub === sub)?.email || sub;
  // A person in a sentence: "You", or their name's first word ("dana.okafor@…" → "Dana").
  const firstName = (who: string) => {
    if (who === "person" || (!!me && who === me) || who === teammate?.me) return "You";
    const name = nameOf(who).split("@")[0].split(/[\s._-]+/)[0] ?? who;
    return name ? name[0].toUpperCase() + name.slice(1) : who;
  };
  const authorName = teammate ? (teammate.author ? firstName(teammate.author) : "the author") : "you";
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
  const bodyOf = (section: string) => (section === LEDE ? plan.lede : plan.sections.find((s) => s.id === section)?.markdown) ?? "";
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

  // A decision's pick: what the person sent once decided; else their draft, their confirmed
  // pick, then the supervisor's. It is settled once confirmed with no other draft over it.
  const drafted = (d: PlanDecisionDTO) => (d.options.some((o) => o.id === picks[d.id]) ? picks[d.id] : undefined);
  const chosen = (d: PlanDecisionDTO) => (open ? drafted(d) : plan.verdict?.picks?.[d.id]) ?? d.confirmed ?? d.mine ?? null;
  const settled = (d: PlanDecisionDTO) => !open || (!!d.confirmed && (drafted(d) ?? d.confirmed) === d.confirmed);
  const next = plan.decisions.find((d) => !settled(d));
  const decided = plan.decisions.filter(settled).length;
  const todo: TodoItem[] = plan.decisions.map((d) => ({ id: d.id, title: d.title, state: settled(d) ? "done" : d === next ? "current" : "todo" }));
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
      const range = { start: toUnits(body, a.start), end: toUnits(body, a.end), thread: t.id, ref: t.id, lit: followed?.thread === String(t.id) };
      out.push(t.kind === "suggestion" ? { ...range, kind: "del", ins: t.after ?? "" } : { ...range, kind: "anchor" });
    }
    const on = draft && !draftStale ? anchorOf(draft) : null;
    if (on?.kind === "text" && on.section === section) {
      const changed = draft?.kind === "suggest" && draftAfter !== null && draftAfter !== on.quote;
      out.push(changed ? { start: on.start, end: on.start + on.quote.length, kind: "del", ins: draftAfter } : { start: on.start, end: on.start + on.quote.length, kind: "sel" });
    }
    return out;
  };

  /* One write to the doc, and only one at a time: the engine answers with the plan as it is now. */
  const send = async (work: () => Promise<Plan>, refused?: (e: unknown) => void): Promise<Plan | null> => {
    if (!writes.begin()) return null;
    setBusy(true);
    setError(null);
    try {
      const now = await work();
      writes.end(now);
      return now;
    } catch (e) {
      writes.end(null);
      refused?.(e);
      if (alive.current) setError(e instanceof DaemonError && e.code === "gate_unverified" ? message(e) : e instanceof DaemonError && e.status === 409 && e.code !== "gate_closed" && e.code !== "review_closed" ? "This design changed since you opened it (a new revision, or it was already decided). It is being read again."
        : e instanceof DaemonError && e.status === 403 ? `The Library refused this for your account: ${message(e)}` : `Couldn't send: ${message(e)}`);
      return null;
    } finally {
      if (alive.current) setBusy(false);
    }
  };
  const write = async (work: () => Promise<Plan>) => (await send(work)) !== null;
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
    setDrafting({ draft: next, source: sourceOf(anchorOf(next)) });
    setDraftAfter(null);
    setPicked(null);
    setTab("comments");
    window.getSelection()?.removeAllRanges();
  };
  const offer = (kind: "comment" | "ask" | "suggest") => {
    if (!picked) return;
    if (picked.kind === "diagram") return begin({ kind: kind === "ask" ? "ask" : "comment", anchor: { kind: "diagram", section: picked.section }, where: `Figure ${figures.get(picked.section) ?? "?"}` });
    const { section, start, quote } = picked;
    if (kind === "suggest") return begin({ kind, section, start, before: quote, where: partName(section) });
    begin({ kind, anchor: { kind: "text", section, start, quote }, where: partName(section) });
  };
  const comment = (anchor: PlanAnchor, on: string) => begin({ kind: "comment", anchor, where: on });
  const setDraft = (_: null) => {
    setDrafting(null);
    setDraftAfter(null);
  };
  // The draft's offsets are the screen's (UTF-16); the engine is sent code points. A draft whose
  // text changed under it (a newer revision) isn't sent: its range no longer says what was meant.
  const sendDraft = async (text: string, after: string) => {
    if (!draft || draftStale) return;
    const on = anchorOf(draft);
    const start = on.kind === "text" ? toPoints(bodyOf(on.section), on.start) : 0;
    const sent = draft.kind === "suggest"
      ? () => source.suggest(plan.rev, { section: draft.section, start, before: draft.before, after }, text)
      : () => source.comment(plan.rev, on.kind === "text" ? { ...on, start } : on, text, draft.kind === "ask");
    if ((await write(sent)) && alive.current) setDraft(null);
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
    if (!thread || collapsed) return;
    setTab("comments");
    setFollowed({ thread });
  };
  useEffect(() => {
    if (followed) show(document.querySelector(`[data-testid="thread-${followed.thread}"]`), "nearest");
  }, [followed]);
  const jump = (decision: string) => show(document.getElementById(`plan-decision-${decision}`));

  // The bottom bar's actions.
  const target = visual?.answer_agent_id;
  const canAttach = open && !!client?.attach && !!target && visual?.accepts_files === true;
  const upload = async (files: File[]) => {
    setUploading((n) => n + 1);
    setError(null);
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
  const said = !!note.trim() || pending || edited || differs;
  const canRequest = said;
  const drafts = Object.fromEntries(plan.decisions.flatMap((d) => (drafted(d) ? [[d.id, drafted(d)!]] : [])));
  const decide = async (approved: boolean) => {
    if (!client || (approved && reviewBlocked)) return;
    setSending(approved ? "approve" : "changes");
    const now = await send(() => client.decidePlan(plan.id, { rev: plan.rev, approved, picks: drafts, comments: [], note: note.trim(), ...(reviewVersion === undefined ? {} : { version: reviewVersion }), ...(attached.length ? { attachments: attached } : {}) }));
    if (!alive.current) return;
    setSending(null);
    if (now) {
      // What was drafted went with the verdict: the next revision is a fresh read.
      setNote("");
      setAttached([]);
      setPicks({});
    }
  };
  /* The plan becomes a personal doc, then the person's sign-off starts the build. */
  const stopSharing = async () => {
    setSending("approve");
    const now = await send(() => sharing!.stopSharingPlan!(plan.id, plan.rev));
    if (now) await decide(true);
    else if (alive.current) setSending(null);
  };
  const confirm = async () => {
    const option = next && chosen(next);
    if (!next || !option) return;
    setSending("confirm");
    await write(() => source.vote(plan.rev, next.id, option));
    if (alive.current) setSending(null);
  };
  const signOff = async () => {
    if (!source.signOff || reviewBlocked) return;
    setSending("approve");
    await write(() => source.signOff!(plan.rev, reviewVersion));
    if (alive.current) setSending(null);
  };
  const nextOption = next?.options.find((o) => o.id === chosen(next));
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
    plan.queued > 0 && `${plural(plan.queued, "assigned task waits", "assigned tasks wait")} on this design.`,
  ].filter(Boolean).join(" ");
  const signName = frozen ? `Sign off version ${frozen.id}` : "Sign off";

  // A text section: its text with the threads' ranges drawn in, or, being edited, its text where
  // it reads (live) or its markdown in a box.
  const commentButton = (on: () => void, name = "Comment") => canComment && <button type="button" className={`${quiet} -my-0.5 text-xs`} onClick={on}>{name}</button>;
  const nextOptionConfirm = next && (
    <LoadingButton className={`${main} max-w-64`} busy={sending === "confirm"} disabled={busy || !nextOption} title={nextOption ? `Confirm ${nextOption.label} for "${next.title}"` : "Pick one of its options first"} onClick={() => void confirm()}>
      <span className="truncate">{nextOption ? `Confirm ${shortName(nextOption.label)}` : "Confirm"}</span>
    </LoadingButton>
  );
  // Editing opens a section where it reads; Suggesting is done on a highlight, as a tracked change.
  const editControl = (section: string, title: string) => mode === "editing" && editing?.section !== section && (
    <button type="button" className={`${quiet} -my-0.5 text-xs`} aria-label={`Edit ${title}`} disabled={!!editing} onClick={() => setEditing({ section, text: bodyOf(section), source: bodyOf(section) })}>
      Edit
    </button>
  );
  // A doc in team review, in Editing mode, is edited live with everyone writing in it: its text
  // is the daemon's live document (DocEditor), each change going out as it is typed.
  const live = mode === "editing" && useLiveReview ? docLive : null;
  const myName = teammate?.me ? nameOf(teammate.me) : me ? nameOf(me) : "Author";
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
  const textBody = (section: string, title: string) => editing?.section === section ? (live ? liveEditor(section, title, "page") : editBox(title)) : (
    <div data-section={section} className="grid gap-2.5" onClick={() => mode === "editing" && !editing && !window.getSelection()?.toString() && setEditing({ section, text: bodyOf(section), source: bodyOf(section) })}>
      <DocText body={bodyOf(section)} marks={marksOf(section)} />
    </div>
  );
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
  const reviewerNames = signers.filter((r) => r.required).map((r) => (r.sub ? firstName(r.sub) : r.name ?? "")).filter(Boolean);

  // The status line.
  const stage = !open && !teammate ? (plan.state === "approved" ? (gate ? "Review closed" : "Signed off") : "Changes requested")
    : frozen ? "Locked for sign-off"
    : gate ? (gate.state === "ready" ? "Ready to build" : gate.state === "changes_requested" ? "Changes requested" : policyHeld ? "Blocked by org policy" : "In team review") : "Needs your sign-off";
  const statusLine = teammate?.signedOff ? `You signed off ${frozen ? `version ${frozen.id}` : `r${plan.rev}`}`
    : frozen && gate ? `Version ${frozen.id} · ${gate.signed} of ${plural(gate.required, "sign-off")}`
    : gate && useLiveReview && open ? `Still being written · sign-off opens when ${teammate ? authorName : "you"} freeze${teammate ? "s" : ""} a version`
    : teammate ? "Its author builds it once every required reviewer signs off"
    : !open ? (plan.state === "approved" ? "Its workers are building it" : "The supervisor is revising it")
    : gate ? "Build starts when every required reviewer signs off" : "Nothing is built until you sign off";
  const statusReviewers: StatusReviewer[] = signers.map((r) => ({ key: r.sub!, who: r.you ? "person" : nameOf(r.sub!), name: r.you ? "You" : firstName(r.sub!), required: r.required, signed: r.status === "signed_off" }));
  const changedAt = Math.max(plan.created_at, ...(plan.revisions ?? []).map((r) => r.at));
  const byline = `by ${teammate ? (teammate.author ? personName(nameOf(teammate.author)) : "its author") : "you and your supervisor"} · changed ${ago(changedAt)}`;

  // The sign-off sheet, under the decisions: once a version is frozen (or for a doc reviewed without live text).
  const sheetRows: SignOffRow[] = signers.map((r) => ({
    key: r.sub!, who: r.you ? "person" : nameOf(r.sub!), name: r.you ? "You" : personName(nameOf(r.sub!)), required: r.required,
    state: r.status === "signed_off" ? "signed" : r.status === "changes_requested" ? "changes" : r.status === "waived" ? "waived" : "waiting",
  }));
  const sinceRevs = lastRead !== null && lastRead < plan.rev ? (plan.revisions ?? []).filter((r) => r.rev > lastRead).map((r) => ({ rev: r.rev, sections: (r.changed ?? []).map((id) => partName(id)) })) : null;
  const sheet = gate && (frozen || !useLiveReview) ? (
    <SignOffSheet version={frozen ? `version ${frozen.id}` : `rev ${plan.rev}`} rows={sheetRows} since={sinceRevs && lastRead !== null ? { from: lastRead, changes: sinceRevs } : null} onShowChanges={() => setTab("history")} />
  ) : undefined;

  // Who reviews it, and sharing it: over the page from the toolbar's Share, not a row of the header.
  const reviewersPanel = (
    <dl className="m-0 grid grid-cols-[72px_minmax(0,1fr)] gap-x-3 gap-y-[5px] text-[12.5px] leading-[1.6] text-ink-2">
      <Reviewers plan={plan} client={sharing} directory={directory} adminOrg={adminOrg} me={me} scopes={scopes} busy={busy} nameOf={nameOf} write={write} reviewVersion={reviewVersion} reviewBlocked={reviewBlocked} />
    </dl>
  );
  const canShare = !!gate || (!!sharing && plan.state !== "approved" && scopes.some((s) => s.kind !== "user" && s.can_share !== false));
  const presence = docLive && (
    <DocPresence client={docLive} visual={plan.id} isAuthor={!!author} name={myName} onState={setLiveState} onCapability={setLiveCapability}
      mode={mode} stripHost={stripHost} reviewers={reviewerNames} onHistory={() => { if (collapsed) collapse(false); setTab("history"); }} />
  );
  return (
    <>
      {chrome && (
        <ReaderBar lead={chrome.lead} badges={chrome.badges} actions={chrome.actions}
          version={<span className="whitespace-nowrap text-xs text-ink-3" title={`${plan.id} · rendered by brindle from the plan's own text${frozen ? ` · rev ${plan.rev}` : ""}`} data-testid="plan-rev">{versionLabel}</span>}
          presence={presence}
          markdown={{ pressed: raw, onToggle: () => setRaw((r) => !r) }}
          modes={{ current: mode, picker: <ModePicker modes={chrome.modes} current={mode} allowed={(m) => (locked ? m === "viewing" : chrome.allowed(m))} onPick={chrome.onMode} /> }}
          share={canShare && <Popover label="Share" open={sharingOpen} onOpenChange={setSharingOpen} testid="plan-share">{reviewersPanel}</Popover>} />
      )}
      {!chrome && presence && <div className="hidden">{presence}</div>}
      {top}
      <div ref={setStripHost} className="contents" />
      {typing && <FormatBar hint={raw ? "The buttons write the Markdown for you: Bold wraps the selection in **." : undefined} />}
      <div className="flex min-h-0 flex-1">
        <div ref={doc} className="relative min-h-0 min-w-0 flex-1 overflow-auto" data-testid="plan-doc" data-mode={mode} onMouseUp={pick} onKeyUp={pick} onClick={followAnchor}>
          <article className="flex flex-col gap-5 pb-10 pl-14 pr-12 pt-7 font-serif text-[15px] leading-[1.65] text-ink-2">
            <div className="grid gap-3 font-sans">
              <h1 className="m-0 font-serif text-[28px] font-semibold leading-[1.2] tracking-[-0.01em] text-ink">{plan.title}</h1>
              <DocStatus stage={stage} tone={frozen ? "branch" : plan.state === "approved" || gate?.state === "ready" ? "pass" : "sap"} line={statusLine}
                reviewers={statusReviewers} frozen={!!frozen} byline={byline} workItem={workItem}
                brief={!!plan.ask} briefOpen={briefOpen} onBrief={() => setBriefOpen((o) => !o)}
                state={plan.state} held={policyHeld ? "policy" : undefined} />
              {plan.team_ended && <p className="m-0 text-[12.5px] text-ink-2" data-testid="plan-team-ended">Your Team plan ended, so this is now a personal design doc.</p>}
              {open && gate?.unconfirmed && (
                <div className="grid gap-1 text-[12.5px] text-ink-2" data-testid="plan-unconfirmed" data-reason={gate.unconfirmed.reason} data-held={gate.unconfirmed.approval_held || undefined}>
                  <span>{gate.unconfirmed.message}</span>
                  {gate.unconfirmed.approval_held && <span>Your approval is saved. The build starts by itself once the Library confirms this revision is ready.</span>}
                  {(gate.unconfirmed.reason === "sign_in" || gate.unconfirmed.reason === "other_account") && <LibrarySignIn />}
                  {gate.unconfirmed.can_stop_sharing && !next && sharing?.stopSharingPlan && (
                    <span>
                      <button type="button" className={`${quiet} -my-0.5 text-xs`} disabled={busy} title="Your teammates lose this doc the next time brindle reaches the Library" onClick={() => void stopSharing()}>Stop sharing and build</button>
                    </span>
                  )}
                </div>
              )}
              {open && gate?.approval_refused && <p className="m-0 text-[12.5px] text-sap" data-testid="plan-approval-refused">{gate.approval_refused}</p>}
              {!chrome && reviewersPanel}
              {plan.ask && briefOpen && (
                <p id="plan-ask" className="m-0 flex flex-wrap items-baseline gap-x-2 border-l-2 border-line-strong pl-3 text-[12.5px] text-ink-2" data-testid="plan-ask">
                  <span className="text-ink-3">What the supervisor understood:</span>
                  <span className="max-w-[78ch]">{plan.ask}</span>
                  {commentButton(() => comment({ kind: "section", section: "ask" }, partName("ask")), "Correct it")}
                </p>
              )}
            </div>
            {raw ? (
              <DocMarkdown plan={plan} state={markdownState} version={frozen ? `version ${frozen.id}` : undefined}
                editorFor={(s, title) => (live ? liveEditor(s, title, "source") : editing?.section === s ? editBox(title) : null)} />
            ) : <>
            {plan.lede && (
              <div id="plan-lede" className="grid gap-2.5">
                {mode === "editing" && editing?.section !== LEDE && <span className={`${aside} justify-self-end`}>{editControl(LEDE, "the overview")}</span>}
                {textBody(LEDE, "the overview")}
              </div>
            )}
            {plan.sections.map(section)}
            {plan.decisions.map((d, i) => {
              const mine = settled(d) ? (open ? "confirmed" : null) : d === next ? "yours to take next" : null;
              return (
                <section key={d.id} id={`plan-decision-${d.id}`} aria-labelledby={`plan-title-decision-${d.id}`} data-next={d === next || undefined} className={`grid gap-2.5 border-t pt-4 ${d === next ? "border-pass" : "border-line"}`}>
                  <h3 className={heading}>
                    <span aria-hidden className={margin}>{plan.sections.length + i + 1}</span>
                    <span id={`plan-title-decision-${d.id}`}>{`Decision · ${d.title}`}</span>
                    <span className={aside}>{`decision ${i + 1} of ${plan.decisions.length}${mine ? ` · ${mine}` : ""}`}</span>
                  </h3>
                  <Decision decision={d} chosen={chosen(d)} open={open} onPick={(o) => setPicks((p) => ({ ...p, [d.id]: o }))} />
                  <p className="flex max-w-[70ch] flex-wrap items-baseline gap-x-2 font-sans text-[12.5px] leading-normal text-ink-3">
                    {d.challenge && <span>{`${d.mine ? "Against its own pick: " : ""}${d.challenge}`}</span>}
                    {commentButton(() => comment({ kind: "decision", decision: d.id }, decisionName(d.id)))}
                  </p>
                </section>
              );
            })}
            </>}
          </article>
          {picked && (
            <div role="toolbar" aria-label="For the highlighted text" className="absolute z-10 inline-flex -translate-y-[calc(100%+6px)] gap-0.5 rounded-md border border-line-strong bg-canvas-2 p-[3px] font-sans text-xs shadow-lg" style={{ left: picked.x, top: picked.y }}
              onMouseDown={(e) => e.preventDefault()} onMouseUp={(e) => e.stopPropagation()}>
              {canComment && <button type="button" className="cursor-pointer rounded-[3px] bg-branch-soft px-[9px] py-0.5 font-medium text-branch" onClick={() => offer("comment")}>Comment</button>}
              {picked.kind === "text" && plan.state !== "approved" && canSuggest && <button type="button" className="cursor-pointer rounded-[3px] px-[9px] py-0.5 text-ink-2 hover:bg-surface-2" onClick={() => offer("suggest")}>Suggest edit</button>}
              {canComment && <button type="button" className="cursor-pointer rounded-[3px] px-[9px] py-0.5 text-ink-2 hover:bg-surface-2" onClick={() => offer("ask")}>{teammate?.author ? `Ask ${authorName}` : "Ask the supervisor"}</button>}
            </div>
          )}
        </div>
        {columnOpen ? (
          <Review plan={plan} tab={draft ? "comments" : tab} onTab={setTab} todo={todo} decided={decided} onJump={jump} where={where} draft={draft} draftStale={draftStale} busy={busy}
            onDraftSend={(text, after) => void sendDraft(text, after)} onDraftCancel={() => setDraft(null)} onDraftAfter={setDraftAfter}
            replaceBox={(after, setAfter) => <FormatTextarea className={`${field} min-h-16 w-full text-[12.5px]`} aria-label="Replace with" autoFocus value={after} onValue={setAfter} />}
            actions={actions} limits={teammate ? { reply: canComment, resolve: (t) => canComment && t.author === "person" } : undefined} onShow={showThread} shared={sharedFiles} onCollapse={() => { setDraft(null); collapse(true); }}
            active={followed ? Number(followed.thread) : null} judge={teammate ? authorName : undefined} sheet={sheet}
            history={history} historyRev={historyRev} onHistoryPick={setHistoryRev} workItems={workItems} />
        ) : (
          <ReviewStrip comments={openThreads.length} decisions={plan.decisions.length - decided} onOpen={() => collapse(false)} />
        )}
      </div>
      <footer className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-2.5 border-t border-line-strong bg-canvas-2 px-s2 py-3" data-testid="plan-verdict">
        {draft?.kind === "suggest" ? (
          <>
            <div className="grid min-w-0 gap-0.5" data-testid="plan-next" data-state="suggesting">
              <span className="flex items-center gap-2.5 text-sm font-semibold text-ink">
                <span className={`${label} text-[11px]`}>Suggesting</span>
                <span className="truncate">{draftAfter !== null && draftAfter !== draft.before ? "1 change not sent" : "Type the new text in its card"}</span>
              </span>
              <span className="text-xs text-ink-3">{teammate ? `Your edits are suggestions. ${authorName} accepts or rejects each one.` : "Your edits are suggestions. You accept or reject each one."}</span>
            </div>
            <div className="flex items-center gap-2">
              <button type="button" className={quiet} onClick={() => setDraft(null)}>Discard all</button>
              <button type="button" className={main} disabled={busy || draftStale || draftAfter === null || draftAfter === draft.before}
                onClick={() => doc.current?.ownerDocument.querySelector<HTMLFormElement>('[data-testid="thread-draft"]')?.requestSubmit()}>Send 1 suggestion</button>
            </div>
          </>
        ) : live && editing ? (
          <>
            <div className="grid min-w-0 gap-0.5" data-testid="plan-next" data-state="editing">
              <span className="flex items-center gap-2.5 text-sm font-semibold text-ink">
                <span className={`${label} text-[11px]`}>Editing</span>
                <span className="truncate">{partName(editing.section)}</span>
              </span>
              <span className="text-xs text-ink-3">Sign-offs wait for a frozen version: nobody signs text that is still moving.</span>
            </div>
            <div className="flex items-center gap-2">
              <button type="button" className={button} onClick={() => setEditing(null)}>Done editing</button>
            </div>
          </>
        ) : teammate ? (
          <>
            <div className="grid min-w-0 gap-0.5" data-testid="plan-next">
              <span className="flex items-center gap-2.5 text-sm font-semibold text-ink">
                <span className={`${label} text-[11px]`}>Next</span>
                <span className="truncate">{next ? next.title : teammate.signedOff ? (frozen ? `You signed off version ${frozen.id}` : `You signed off r${plan.rev}`) : teammate.rights.signOff ? (frozen ? signName : "Sign off this revision") : gateWhy || "Read and comment"}</span>
              </span>
              <span className="text-xs text-ink-3">{summary}</span>
            </div>
            <div className="flex items-center gap-2">
              {next && <button type="button" className={quiet} onClick={() => jump(next.id)}>Jump to it</button>}
              {next ? nextOptionConfirm : teammate.rights.signOff && !teammate.signedOff && (
                <LoadingButton className={main} busy={sending === "approve"} disabled={busy || reviewBlocked} data-testid="plan-sign-off" onClick={() => void signOff()}>{signName}</LoadingButton>
              )}
            </div>
          </>
        ) : open ? (
          <>
            <div className="grid min-w-0 gap-0.5" data-testid="plan-next">
              <span className="flex items-center gap-2.5 text-sm font-semibold text-ink">
                <span className={`${label} text-[11px]`}>Next</span>
                <span className="truncate">{next ? next.title : gate ? (gate.state === "ready" ? "Ready to build" : gateWhy) : "Sign off this design"}</span>
              </span>
              <span className="text-xs text-ink-3">
                {summary}
                {sharedFiles.size > 0 && <span className="text-sap">{`${summary ? " " : ""}${plural(sharedFiles.size, "file is", "files are")} in more than one worker.`}</span>}
              </span>
            </div>
            <div className="flex items-center gap-2">
              {next && <button type="button" className={quiet} onClick={() => jump(next.id)}>Jump to it</button>}
              <LoadingButton className={button} busy={sending === "changes"} disabled={busy || !canRequest || uploading > 0}
                title={canRequest ? "Send it back to the supervisor for a new revision, with your notes" : "Comment, add a note or pick differently first"} onClick={() => void decide(false)}>
                Request changes
              </LoadingButton>
              {/* One main action at a time: the next decision's confirmation, then the sign-off. */}
              {next ? nextOptionConfirm : (
                <LoadingButton className={main} busy={sending === "approve"} disabled={busy || reviewBlocked || uploading > 0 || (!!gate && gate.state !== "ready")} title={gate && gate.state !== "ready" ? gateWhy : undefined}
                  data-testid="plan-build" data-gate={gate?.state} onClick={() => void decide(true)}>
                  {gate ? "Start building" : "Sign off"}
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
        ) : (
          <div role="status" className="col-span-full flex min-w-0 flex-col gap-0.5 text-sm" data-testid="plan-decided">
            <p>{plan.state === "approved" ? `You signed off r${plan.rev}. Its workers start now.` : "You asked for changes. The supervisor is revising the design."}</p>
            {plan.verdict?.note && <p className="text-ink-2"><span className="text-ink-3">your note · </span>{plan.verdict.note}</p>}
            {plan.verdict?.comments?.map((c, i) => (
              <p key={i} className="text-ink-2">
                <span className="text-ink-3">on {partName(c.section)} · </span>
                {c.text}
              </p>
            ))}
          </div>
        )}
        {error && error !== gate?.unconfirmed?.message && <p role="alert" className="col-span-full text-xs text-sap">{error}</p>}
      </footer>
    </>
  );
}
