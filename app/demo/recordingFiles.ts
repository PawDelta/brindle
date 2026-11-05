/* What the recording's three workers change (recording.ts): each file as
   main has it (null: new) and as the session's branch has it once their
   branches merge. The PR's diff is made from these, and so is every count
   the workers' panes print (lines read and written, additions and
   removals, the line numbers of an edit, tests run, a commit's stat), so a
   frame of a pane and the PR agree. */

import { README_BASE, README_FILE } from "../shots/fixtures";

export interface Change {
  branch: "desktop/ux-revamp" | "desktop/shots" | "desktop/inbox";
  before: string | null;
  after: string;
}

const file = (lines: string[]) => lines.join("\n") + "\n";

const SESSION_BEFORE = file([
  "/* The session view: its agents and the selected one's terminal. */",
  'import { useState } from "react";',
  'import type { AgentDTO, GoalDTO } from "../../api/schema";',
  'import { AgentList } from "../../components/AgentList";',
  'import { Terminal } from "../../components/Terminal";',
  "",
  "export interface SessionScreenProps {",
  "  agents: AgentDTO[];",
  "  goal: GoalDTO | null;",
  "}",
  "",
  "export function SessionScreen({ agents, goal }: SessionScreenProps) {",
  "  const [selected, setSelected] = useState<string | null>(agents[0]?.id ?? null);",
  "  return (",
  '    <main className="session">',
  '      <header className="session-head">{goal?.goal ?? "No goal set"}</header>',
  "      <AgentList agents={agents} selected={selected} onSelect={setSelected} />",
  "      <Terminal agent={selected} />",
  "    </main>",
  "  );",
  "}",
]);

const SESSION_AFTER = file([
  "/* The session view: the goal panel above the agent tree, and the selected",
  "   agent's terminal or its branch's diff. */",
  'import { useState } from "react";',
  'import type { AgentDTO, GoalDTO } from "../../api/schema";',
  'import { AgentTree } from "../../components/AgentTree";',
  'import { BranchPanel } from "../../components/BranchPanel";',
  'import { GoalPanel } from "../../components/GoalPanel";',
  'import { Terminal } from "../../components/Terminal";',
  'import { sessionTree } from "./tree";',
  "",
  "export interface SessionScreenProps {",
  "  agents: AgentDTO[];",
  "  goal: GoalDTO | null;",
  "}",
  "",
  'type Tab = "terminal" | "diff";',
  "",
  "export function SessionScreen({ agents, goal }: SessionScreenProps) {",
  "  const [selected, setSelected] = useState<string | null>(agents[0]?.id ?? null);",
  '  const [tab, setTab] = useState<Tab>("terminal");',
  "  return (",
  '    <main className="session">',
  "      <GoalPanel goal={goal} />",
  "      <AgentTree nodes={sessionTree(agents)} selected={selected} onSelect={setSelected} />",
  '      <nav className="session-tabs" role="tablist">',
  '        <button role="tab" aria-selected={tab === "terminal"} onClick={() => setTab("terminal")}>Terminal</button>',
  '        <button role="tab" aria-selected={tab === "diff"} onClick={() => setTab("diff")}>Diff</button>',
  "      </nav>",
  '      {tab === "terminal" ? <Terminal agent={selected} /> : <BranchPanel agent={selected} />}',
  "    </main>",
  "  );",
  "}",
]);

const SESSION_TEST_BEFORE = file([
  'import { render, screen } from "@testing-library/react";',
  'import { describe, expect, it } from "vitest";',
  'import { agents, goal } from "../../test/fixtures";',
  'import { SessionScreen } from "./SessionScreen";',
  "",
  'describe("SessionScreen", () => {',
  '  it("lists the session\'s agents", () => {',
  "    render(<SessionScreen agents={agents} goal={goal} />);",
  '    expect(screen.getAllByRole("listitem")).toHaveLength(agents.length);',
  "  });",
  "",
  '  it("shows the first agent\'s terminal", () => {',
  "    render(<SessionScreen agents={agents} goal={goal} />);",
  '    expect(screen.getByTestId("terminal").dataset.agent).toBe(agents[0].id);',
  "  });",
  "});",
]);

const SESSION_TEST_AFTER = file([
  'import { fireEvent, render, screen } from "@testing-library/react";',
  'import { describe, expect, it } from "vitest";',
  'import { agents, goal } from "../../test/fixtures";',
  'import { SessionScreen } from "./SessionScreen";',
  "",
  'describe("SessionScreen", () => {',
  '  it("puts the goal panel above the agent tree", () => {',
  "    render(<SessionScreen agents={agents} goal={goal} />);",
  '    const [panel, tree] = [screen.getByTestId("goal-panel"), screen.getByRole("tree")];',
  "    expect(panel.compareDocumentPosition(tree) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();",
  "  });",
  "",
  '  it("nests each worker under the agent that started it", () => {',
  "    render(<SessionScreen agents={agents} goal={goal} />);",
  '    expect(screen.getAllByRole("treeitem")).toHaveLength(agents.length);',
  "  });",
  "",
  '  it("shows the first agent\'s terminal", () => {',
  "    render(<SessionScreen agents={agents} goal={goal} />);",
  '    expect(screen.getByTestId("terminal").dataset.agent).toBe(agents[0].id);',
  "  });",
  "",
  '  it("keys the diff tab by the selected worker", () => {',
  "    render(<SessionScreen agents={agents} goal={goal} />);",
  "    fireEvent.click(screen.getByText(agents[1].id));",
  '    fireEvent.click(screen.getByRole("tab", { name: "Diff" }));',
  '    expect(screen.getByTestId("branch-panel").dataset.agent).toBe(agents[1].id);',
  "  });",
  "});",
]);

const SHOTS = file([
  "#!/usr/bin/env node",
  "/* Renders every scene of the preview entry, light and dark, into",
  "   desktop/shots/out/<scene>-<theme>.png with Playwright's Chromium. */",
  'import { mkdirSync } from "node:fs";',
  'import { join } from "node:path";',
  'import { pathToFileURL } from "node:url";',
  'import { parseArgs } from "node:util";',
  'import { chromium } from "playwright";',
  "",
  'const SCENES = ["session", "inbox", "settings", "new-session"];',
  'const { values } = parseArgs({ options: { scene: { type: "string" }, out: { type: "string", default: "desktop/shots/out" } } });',
  'const scenes = values.scene ? values.scene.split(",") : SCENES;',
  'const entry = pathToFileURL("desktop/shots/dist/preview.html").href;',
  "mkdirSync(values.out, { recursive: true });",
  "",
  "const browser = await chromium.launch();",
  'for (const theme of ["light", "dark"]) {',
  "  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, colorScheme: theme });",
  "  for (const scene of scenes) {",
  "    await page.goto(`${entry}?scene=${scene}`);",
  '    await page.locator("[data-ready]").waitFor();',
  "    const out = join(values.out, `${scene}-${theme}.png`);",
  "    await page.screenshot({ path: out });",
  "    console.log(out);",
  "  }",
  "  await page.close();",
  "}",
  "await browser.close();",
]);

const PREVIEW = file([
  "/* The preview entry the shots render: the real app over the fake daemon, one scene per ?scene=. */",
  'import { createRoot } from "react-dom/client";',
  'import App from "../src/App";',
  'import { FakeDaemon } from "../src/test/fakeDaemon";',
  'import { data } from "../src/test/fixtures";',
  "",
  'const ROUTES: Record<string, string> = { session: "#/session/sup-42", inbox: "#/inbox", settings: "#/settings", "new-session": "#/new" };',
  'const scene = new URLSearchParams(location.search).get("scene") ?? "session";',
  "const daemon = new FakeDaemon(structuredClone(data));",
  'location.hash = ROUTES[scene] ?? "#/";',
  "",
  'createRoot(document.getElementById("root")!).render(<App client={daemon.client()} />);',
  'requestAnimationFrame(() => document.body.setAttribute("data-ready", ""));',
]);

const SHOTS_README = file([
  "# Renderings",
  "",
  "Every screen, light and dark, rendered from a preview entry over the fake daemon:",
  "",
  "```sh",
  "node desktop/shots/shots.mjs                # every scene into desktop/shots/out/",
  "node desktop/shots/shots.mjs --scene inbox",
  "```",
  "",
  "Playwright's Chromium is fetched by npx on the first run.",
]);

const GROUP = file([
  "/* The inbox's items by session, the newest session first. */",
  'import type { InboxItemDTO } from "../../api/schema";',
  "",
  "export interface SessionGroup {",
  "  session: { root: string; goal: string };",
  "  items: InboxItemDTO[];",
  "}",
  "",
  "export function bySession(items: InboxItemDTO[], goals: Record<string, string> = {}): SessionGroup[] {",
  "  const groups = new Map<string, SessionGroup>();",
  "  for (const item of items) {",
  '    const root = item.root_id ?? "";',
  "    const group = groups.get(root) ?? { session: { root, goal: goals[root] ?? root }, items: [] };",
  "    group.items.push(item);",
  "    groups.set(root, group);",
  "  }",
  "  return [...groups.values()].reverse();",
  "}",
]);

const INBOX_BEFORE = file([
  "/* The Inbox: every item waiting on the person, as a card. */",
  'import type { InboxItemDTO } from "../../api/schema";',
  'import { InboxCard } from "./InboxCard";',
  "",
  "export function InboxScreen({ items }: { items: InboxItemDTO[] }) {",
  "  return (",
  '    <main className="inbox">',
  "      {items.map((item) => <InboxCard key={item.agent_id} item={item} />)}",
  "    </main>",
  "  );",
  "}",
]);

const INBOX_AFTER = file([
  "/* The Inbox: every item waiting on the person, as a card under its session. */",
  'import type { InboxItemDTO } from "../../api/schema";',
  'import { bySession } from "./group";',
  'import { InboxCard } from "./InboxCard";',
  "",
  "export function InboxScreen({ items, goals }: { items: InboxItemDTO[]; goals: Record<string, string> }) {",
  "  return (",
  '    <main className="inbox">',
  "      {bySession(items, goals).map(({ session, items }) => (",
  "        <section key={session.root} aria-label={session.goal}>",
  "          <h2>{session.goal}</h2>",
  "          {items.map((item) => <InboxCard key={item.agent_id} item={item} linkTo={item.agent_id} />)}",
  "        </section>",
  "      ))}",
  "    </main>",
  "  );",
  "}",
]);

const NOTIFY_BEFORE = file([
  "/* The tray badge: how many inbox items wait on the person. */",
  'import type { InboxItemDTO } from "../../api/schema";',
  "",
  "export function badgeCount(items: InboxItemDTO[]): number {",
  '  return items.filter((i) => i.kind !== "org_notice").length;',
  "}",
]);

const NOTIFY_AFTER = file([
  "/* The tray badge: the inbox items waiting on the person, counted as the",
  "   Inbox lists them (a card each, under its session). */",
  'import type { InboxItemDTO } from "../../api/schema";',
  'import { bySession } from "./group";',
  "",
  "export function badgeCount(items: InboxItemDTO[]): number {",
  '  return bySession(items.filter((i) => i.kind !== "org_notice")).reduce((n, group) => n + group.items.length, 0);',
  "}",
]);

const NOTIFY_TEST_BEFORE = file([
  'import { describe, expect, it } from "vitest";',
  'import { notice, permission, plan } from "../../test/fixtures";',
  'import { badgeCount } from "./notify";',
  "",
  'describe("badgeCount", () => {',
  '  it("leaves the org\'s notices out", () => {',
  "    expect(badgeCount([permission, plan, notice])).toBe(2);",
  "  });",
  "});",
]);

const NOTIFY_TEST_AFTER = file([
  'import { describe, expect, it } from "vitest";',
  'import { notice, permission, plan } from "../../test/fixtures";',
  'import { badgeCount } from "./notify";',
  "",
  'describe("badgeCount", () => {',
  '  it("leaves the org\'s notices out", () => {',
  "    expect(badgeCount([permission, plan, notice])).toBe(2);",
  "  });",
  "",
  '  it("counts every session\'s cards, as the Inbox lists them", () => {',
  '    const other = { ...plan, agent_id: "f1", root_id: "sup-39" };',
  "    expect(badgeCount([permission, plan, other])).toBe(3);",
  "  });",
  "});",
]);

export const CHANGES: Record<string, Change> = {
  "desktop/src/screens/Session/SessionScreen.tsx": { branch: "desktop/ux-revamp", before: SESSION_BEFORE, after: SESSION_AFTER },
  "desktop/src/screens/Session/SessionScreen.test.tsx": { branch: "desktop/ux-revamp", before: SESSION_TEST_BEFORE, after: SESSION_TEST_AFTER },
  "desktop/shots/shots.mjs": { branch: "desktop/shots", before: null, after: SHOTS },
  "desktop/shots/preview.tsx": { branch: "desktop/shots", before: null, after: PREVIEW },
  "desktop/shots/README.md": { branch: "desktop/shots", before: null, after: SHOTS_README },
  // The demo's own README change (the session's files in demo.tsx), the shots worker's link to its README.
  "desktop/README.md": { branch: "desktop/shots", before: README_BASE, after: README_FILE },
  "desktop/src/screens/Inbox/group.ts": { branch: "desktop/inbox", before: null, after: GROUP },
  "desktop/src/screens/Inbox/InboxScreen.tsx": { branch: "desktop/inbox", before: INBOX_BEFORE, after: INBOX_AFTER },
  "desktop/src/screens/Inbox/notify.ts": { branch: "desktop/inbox", before: NOTIFY_BEFORE, after: NOTIFY_AFTER },
  "desktop/src/screens/Inbox/notify.test.ts": { branch: "desktop/inbox", before: NOTIFY_TEST_BEFORE, after: NOTIFY_TEST_AFTER },
};
