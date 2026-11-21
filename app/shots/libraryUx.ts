/* The Settings › Library scenes of the Library and sharing UX review (dev only, shots/library-ux-shots.mjs):
   the person's Library switches as the engine answers them (analysis_docs.switch_states), over the fake
   daemon, which has no route for them.
     settings-library          a Pro person's defaults: plans on this Mac, every other switch off
     settings-library-servers  the same, then "brindle servers" pressed: only the location changes
     settings-library-locked   an Enterprise member whose org enforces some of them
     settings-library-admin    that org's admin: the same switches, plus Team destination */

import type { DaemonClient, DocSwitch, DocSwitchName } from "../src/api/client";
import type { FakeDaemon } from "../src/test/fakeDaemon";

const NAMES: DocSwitchName[] = ["store", "share", "assist", "prompt_analysis", "import_upload"];
const ORG = "org_6e83ecddb131dfc35d43d5a8";
const free = (name: DocSwitchName, on = false): DocSwitch => ({ name, on, choice: on ? "on" : null, locked: false, org: null, reason: null });
/* As switch_states words an enforced switch: the org's id, not its name. */
const enforced = (name: DocSwitchName, on: boolean): DocSwitch =>
  ({ name, on, choice: null, locked: true, org: on ? "enforced_on" : "enforced_off", reason: `Set by your organization's admin (${ORG}).` });

export const libraryUxScene = (scene: string) => scene.startsWith("settings-library");

/* Before the app mounts: whose account it is. */
export function prepareLibraryUx(daemon: FakeDaemon, scene: string): void {
  if (scene !== "settings-library-locked" && scene !== "settings-library-admin") return;
  const admin = scene === "settings-library-admin";
  daemon.data.account = {
    ...daemon.data.account, name: admin ? "cto@sovereign.example" : "sam@sovereign.example", plan: "enterprise", org_id: ORG, role: admin ? "admin" : "member", seats: 400,
    features: ["airgap", "audit", "managed_rollout", "cost_centers", "ci", "team", "federation", "org_budgets", "cost", "learning", "services", "multi_repo", "coordinator", "settings_sync", "ci_fix"],
    orgs: [{ id: ORG, name: "Sovereign Systems", role: admin ? "admin" : "member", plan: "enterprise", personal: false, current: true }],
  };
}

export function installLibraryUx(client: DaemonClient, scene: string): void {
  let switches: DocSwitch[] = scene === "settings-library-locked"
    ? [enforced("store", true), enforced("share", true), free("assist", true), enforced("prompt_analysis", false), free("import_upload")]
    : NAMES.map((name) => free(name));
  Object.assign(client, {
    docSwitches: async () => switches,
    /* As set_switch does: the one switch named, nothing else; a location change answers how many plans moved. */
    setDocSwitch: async (name: DocSwitchName, on: boolean) => {
      switches = switches.map((s) => (s.name === name ? { ...s, on, choice: on ? "on" : "off" } : s));
      return { switches, ...(name === "store" ? { moved: 6, left: 0 } : {}) };
    },
    docDestination: async () => ({ kind: null, target_name: null, connected_at: null }),
  });
}

/* Once the screen is up: the Library section in view, and for -servers its location switched. */
export function playLibraryUx(scene: string): void {
  const section = document.querySelector<HTMLElement>('[data-testid="doc-switches"]');
  if (scene === "settings-library-servers") [...(section?.querySelectorAll<HTMLButtonElement>('[role="radio"]') ?? [])].find((b) => b.textContent === "brindle servers")?.click();
  section?.scrollIntoView({ block: "start" });
}
