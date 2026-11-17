/* New session with no project yet (the owner-approved first page, option
   A "Show it first"): the practice run first, then the person's own code.
   A hero says what brindle does, with a replay of the practice run's four
   steps (Plan, Build, Review, Merge, lighting in turn) beside its one
   primary button, Watch the practice run (POST /demo, as the row under the
   composer starts it later: Onboarding/Demo). Under it, "Or start with
   your own code": Open a folder and Clone as two equal rows
   (ProjectPicker's rows); Clone expands in place, and while it is open the
   hero dims so Clone is the one green action. Once a project exists only
   the composer's small practice row is left (NewSession). */

import { useState } from "react";
import type { DaemonClient, Repo } from "../../api/client";
import type { PickFolder } from "../../api/open";
import { PlayIcon } from "../../components/icons";
import { ProjectPicker } from "../../components/ProjectPicker";
import { Dot, type DotTone } from "../../components/Sidebar";
import { button, h2, label, main } from "../../components/ui";
import { useDemo } from "../Onboarding/Demo";

/* The replay's steps: what each shows, as the practice run goes. */
const STEPS: { name: string; rows: { tone?: DotTone; text: string }[] }[] = [
  { name: "Plan", rows: [{ tone: "work", text: "supervisor" }, { text: "splits it up" }] },
  { name: "Build", rows: [{ tone: "work", text: "slugify" }, { tone: "work", text: "wrap" }] },
  { name: "Review", rows: [{ tone: "wait", text: "reviewer" }, { text: "each branch" }] },
  { name: "Merge", rows: [{ tone: "done", text: "2 of 2" }, { text: "tests pass" }] },
];

const FACTS = ["A few minutes", "A sample repo, two failing tests", "On the CLI you just signed in to"];

export function FirstRun({ client, pickFolder, refresh, onStarted, onPicked, start }: {
  client: DaemonClient;
  pickFolder?: PickFolder;
  refresh?: () => Promise<void>;
  /* The practice run started: its session. */
  onStarted(root: string, repo: string): void;
  /* A project opened or cloned. */
  onPicked(repo: Repo): void;
  /* Arrived from a finished practice run's Open a folder… or Clone a repository…. */
  start?: "open" | "clone";
}) {
  const demo = useDemo(client, onStarted, refresh);
  const [cloning, setCloning] = useState(start === "clone");
  return (
    <section aria-label="Welcome" className="mx-auto flex w-full max-w-[672px] flex-col pb-s6 pt-[120px]">
      <span className={label}>Welcome to brindle</span>
      <h1 className="mt-2.5 text-[30px] font-semibold leading-tight tracking-[-0.02em]">Watch a team of agents fix a repo.</h1>
      <p className="mt-2.5 max-w-[610px] text-ink-2">
        A supervisor splits the work, developers each take a branch in their own worktree, and a reviewer checks every branch before it merges. Try it on a practice repo first: it lives in brindle's own folder, so your projects stay as they are.
      </p>
      <div
        className={`mt-7 grid grid-cols-[minmax(0,1fr)_264px] overflow-hidden rounded-[var(--r)] border border-line-strong transition-opacity ${cloning ? "opacity-55" : ""}`}
        data-testid="practice-hero"
        data-state={cloning ? "dimmed" : "lit"}
      >
        <div className="border-r border-line bg-canvas-2 px-5 py-[18px]">
          <span className={label}>What you'll see · replay</span>
          <ol className="mt-3 grid grid-cols-4 gap-2" aria-label="The practice run's steps">
            {STEPS.map((step, i) => (
              <li key={step.name} className="replay-step min-h-[104px] rounded-[var(--r)] border border-line px-2.5 py-[9px]">
                <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-ink-3">
                  {i + 1} {step.name}
                </span>
                <div className="mt-2 flex flex-col gap-[5px]">
                  {step.rows.map((row) => (
                    <span key={row.text} className={`flex items-center gap-1.5 whitespace-nowrap text-[11.5px] ${row.tone ? "text-ink-2" : "text-ink-3"}`}>
                      {row.tone && <Dot tone={row.tone} className="!size-1.5" />}
                      {row.text}
                    </span>
                  ))}
                </div>
              </li>
            ))}
          </ol>
          <div className="replay-scrub relative mt-3.5 h-1 rounded-sm bg-line" aria-hidden>
            <i className="absolute inset-y-0 left-0 w-[12%] rounded-sm bg-branch" />
          </div>
        </div>
        <div className="flex flex-col gap-3 p-5">
          <div className={h2}>Practice run</div>
          <ul className="mb-auto flex flex-col gap-1.5 text-[12.5px] text-ink-2">
            {FACTS.map((fact) => (
              <li key={fact} className="flex items-center gap-2">
                <Dot tone="done" className="!size-1.5" />
                {fact}
              </li>
            ))}
          </ul>
          <button type="button" className={`${cloning ? button : main} min-h-9 px-4 text-sm`} disabled={demo.busy} onClick={() => void demo.start()}>
            <PlayIcon />
            {demo.busy ? "Starting the practice run…" : "Watch the practice run"}
          </button>
        </div>
      </div>
      <div className="mt-s2 empty:hidden">{demo.problems}</div>
      <div className="mb-3.5 mt-9 flex items-center gap-3 after:h-px after:flex-1 after:bg-line">
        <span className={label}>Or start with your own code</span>
      </div>
      <ProjectPicker client={client} pickFolder={pickFolder} onPicked={onPicked} layout="rows" start={start} onCloning={setCloning} />
    </section>
  );
}
