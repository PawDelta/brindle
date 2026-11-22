/* Standalone production panels over in-memory shell and sharing services. */
import { useEffect } from "react";
import { createRoot } from "react-dom/client";
import { DaemonClient, type DesignShareLink, type DocScope, type Plan, type ShareAccess, type SharePerson } from "../src/api/client";
import { PtyViewsProvider, type PtyViews } from "../src/api/ptyView";
import { TerminalPanel, TerminalProvider, TerminalToggle, useTerminals } from "../src/components/TerminalPanel";
import { applyTheme, THEME_KEY } from "../src/components/ThemeToggle";
import { DesignDoc, type Writes } from "../src/screens/Session/Visuals/PlanReader";
import { SharePanel, type SharePeopleTarget } from "../src/screens/Session/Visuals/SharePanel";
import { ShareLinkPanel, type ShareLinkTarget } from "../src/screens/Session/Visuals/ShareLink";
import type { DocSource } from "../src/screens/Session/Visuals/docSource";
import "../src/styles.css";

const params = new URLSearchParams(location.search);
const theme = params.get("theme") === "light" ? "light" : "dark";
localStorage.setItem(THEME_KEY, theme);
applyTheme(theme);
const scopes: DocScope[] = [
  { kind: "org", id: "pawdelta", name: "PawDelta", can_share: true },
  { kind: "group", id: "platform", name: "Platform engineering", can_share: true },
];
const people: SharePerson[] = [
  { id: "alex", display_name: "Alexandria Montgomery-Worthington", email: "alexandria.montgomery-worthington@platform.example.com", role: "admin", teams: [{ id: "platform", name: "Platform engineering" }], on_plan: true },
  { id: "sam", display_name: "Sam Rivera", email: "sam.rivera@platform.example.com", role: "member", teams: [], on_plan: true },
  { id: "jo", display_name: "Josephine van der Meer-Sutherland", email: "josephine.van.der.meer-sutherland@engineering.example.com", role: "member", teams: [], on_plan: false },
  { id: "lee", display_name: "Lee Chen", email: "lee.chen.with.a.long.address@engineering.example.com", role: "member", teams: [], on_plan: false },
];
const nameOf = (id: string) => people.find((p) => p.id === id)?.display_name ?? id;
const plan: Plan = {
  id: "panel-audit", session: "audit", title: "Review the terminal and sharing experience", rev: 1,
  state: "open", can_edit: true, created_at: 1795200000, queued: 0, markdown: "", lede: "A shared design for the platform team.",
  sections: [{ id: "summary", title: "Summary", markdown: "Keep a shell beside the work and invite teammates to review the design. Required reviewers sign off before the build starts." }],
  decisions: [], split: [], threads: [],
  gate: { state: "waiting", required: 1, signed: 1, reviewers: [
    { sub: "alex", required: true, status: "signed_off" },
    { sub: "sam", required: false, status: "waiting" },
  ] },
};
const writes: Writes = { begin: () => true, end: () => {} };
const directory = people.map((p) => ({ sub: p.id, email: p.email, display_name: p.display_name }));
const access: ShareAccess = { shared: true, kept_here: null, scopes: [{ kind: "group", id: "platform" }], people: [
  { sub: "alex", name: null, access: "edit", sign_off: "required" },
  { sub: "sam", name: null, access: "comment", sign_off: "optional" },
] };
const target = (shared: boolean): SharePeopleTarget => ({
  search: async () => ({ people }), access: async () => (shared ? access : { shared: false, kept_here: true, scopes: [], people: [] }),
  invite: async () => plan, change: async () => plan, signOff: async () => plan, revoke: async () => plan, revokeScope: async () => plan,
  shareScope: async () => plan, stopAll: async () => plan,
});
const link: DesignShareLink = { available: true, scoped: true, link: { access: "org", url: "https://pawdelta.com/share/terminal-and-sharing-design", permission: "view", scope: { kind: "team", org_id: "pawdelta", team: "platform" } } };
const links: ShareLinkTarget = { key: plan.id, read: async () => link, copy: async () => link, revoke: async () => ({ available: true, scoped: true }), scopes: async () => ({ scopes }) };
const readOnly = async (): Promise<Plan> => { throw new Error("Read only reviewer"); };
const source: DocSource = { id: plan.id, read: async () => plan, comment: readOnly, reply: readOnly, resolve: readOnly, suggest: readOnly, vote: readOnly };

const shell = new DaemonClient({ baseUrl: "http://panel-audit.invalid", token: "audit-only" });
shell.openShell = async () => ({ terminal: "audit-shell", started: true, server: null, target: "audit:0.0", shell: "zsh" });
shell.closeTerminal = async () => ({ terminal: "audit-shell", closed: true });
const bridge: PtyViews = {
  status: { kind: "ready", tmux: "3.4" }, copy: async () => {}, paste: async () => "", recheck: () => {},
  open: (_at, _size, handlers) => {
    let closed = false;
    queueMicrotask(() => {
      if (closed) return;
      handlers.onData(new TextEncoder().encode([
        "\x1b[32m✓ Build complete\x1b[0m  \x1b[33mwarning: optional integration unavailable\x1b[0m",
        "\x1b[31merror sample\x1b[0m  \x1b[36mplatform terminal\x1b[0m  \x1b[35mshots/terminal-share-panel\x1b[0m",
        "\x1b[32mhannah\x1b[0m \x1b[34m~/Projects/brindle\x1b[0m % ls",
        "\x1b[34mapp\x1b[0m  \x1b[34mdocs\x1b[0m  \x1b[34mscripts\x1b[0m  README.md  pyproject.toml",
        "\x1b[32mhannah\x1b[0m \x1b[34m~/Projects/brindle\x1b[0m % ",
      ].join("\r\n")));
    });
    return { write: () => {}, resize: () => {}, focus: () => {}, close: () => { closed = true; } };
  },
};
function OpenTerminal() {
  const terminals = useTerminals();
  useEffect(() => { terminals?.toggle(); }, []);
  return <><TerminalToggle /><TerminalPanel /></>;
}
function Panels() {
  useEffect(() => { document.documentElement.dataset.ready = "1"; }, []);
  if (params.get("panel") === "terminal") return <PtyViewsProvider value={bridge}><TerminalProvider client={shell} here={{ repo: "/Users/hannah/Projects/brindle", name: "brindle", cwd: "/Users/hannah/Projects/brindle", why: "started at the repo root" }}><main className="flex h-screen flex-col justify-end bg-canvas"><OpenTerminal /></main></TerminalProvider></PtyViewsProvider>;
  const state = params.get("state") ?? "people";
  if (state === "reviewer") return <main className="flex h-screen flex-col bg-canvas"><DesignDoc source={source} plan={{ ...plan, can_edit: false }} mode="viewing" writes={writes} teammate={{ me: "sam", author: "alex", rights: { comment: false, suggest: false, signOff: false }, signedOff: false, directory }} /></main>;
  // A plan not yet shared: the same panel, with your teams and what sharing uploads; no link until it is in the Library.
  if (state === "first" || state === "scopes") return <main className="flex h-screen justify-end bg-canvas"><SharePanel plan={{ ...plan, gate: null }} target={target(false)} nameOf={nameOf} writes={writes} onBusy={() => {}} visible onClose={() => {}}
    teams={scopes} directory={directory} owned /></main>;
  return <main className="flex h-screen justify-end bg-canvas"><SharePanel plan={plan} target={target(true)} nameOf={nameOf} writes={writes} onBusy={() => {}} visible onClose={() => {}}
    teams={scopes} directory={directory} owned>
    <ShareLinkPanel target={links} state={link} onState={() => {}} />
  </SharePanel></main>;
}
createRoot(document.getElementById("root")!).render(<Panels />);
