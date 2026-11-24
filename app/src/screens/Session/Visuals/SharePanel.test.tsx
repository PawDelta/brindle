import { useState } from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { DaemonError, type DesignShareLink, type DocScope, type LibraryPerson, type Plan, type ShareAccess, type SharePerson, type ShareQueued } from "../../../api/client";
import { SharePanel, type SharePeopleTarget } from "./SharePanel";
import { ShareLinkPanel, type ShareLinkTarget } from "./ShareLink";

afterEach(cleanup);

const people: SharePerson[] = ["sam", "lena"].map((id) => ({ id, display_name: id, email: `${id}@example.com`,
  teams: [{ id: "platform", name: "Platform" }], role: "member", on_plan: false, on_plan_role: null }));
const teams: DocScope[] = [{ kind: "group", id: "platform", name: "Platform", can_share: true }, { kind: "org", id: "o1", name: "PawDelta", can_share: true }];
const empty: Plan = { id: "p1", rev: 1, title: "Design", state: "open", decisions: [], split: [], sections: [],
  session: "boss", created_at: 1, lede: "", markdown: "", queued: 0,
  gate: { state: "ready", required: 0, signed: 0, waiting_on: [], reviewers: [] } };

/* The Library as the panel sees it: one person or one scope changes per call. `hold` keeps a call
   open until the test lets it finish; `refuse` makes the named person's next call fail. */
function library(start: Partial<ShareAccess> = {}, signed: string[] = []) {
  const state: ShareAccess = { shared: true, kept_here: null, scopes: [], people: [], ...start };
  const calls: string[] = [];
  const held: (() => void)[] = [];
  const refuse = new Set<string>();
  let hold = false;
  const plan = (): Plan => ({ ...empty, gate: state.shared ? { ...empty.gate!, reviewers: state.people.filter((p) => p.sign_off)
    .map((p) => ({ sub: p.sub, required: p.sign_off === "required", status: signed.includes(p.sub) ? "signed_off" as const : "waiting" as const })) } : null });
  const write = (call: string, sub: string, change: () => void) => new Promise<Plan>((resolve, reject) => {
    const finish = () => {
      calls.push(call);
      if (refuse.delete(sub)) { reject(new Error(`The Library refused ${call}.`)); return; }
      change();
      resolve(plan());
    };
    if (hold) held.push(finish);
    else finish();
  });
  const person = (sub: string) => state.people.find((p) => p.sub === sub)!;
  const target: Required<SharePeopleTarget> = {
    search: async () => ({ people }),
    access: async () => structuredClone(state),
    invite: (sub, access, signOff) => write(`invite ${sub} ${access} ${signOff}`, sub, () => {
      state.shared = true;
      state.people = [...state.people.filter((p) => p.sub !== sub), { sub, name: null, access, sign_off: signOff ? "required" : null }];
    }),
    change: (sub, access) => write(`change ${sub} ${access}`, sub, () => { person(sub).access = access; }),
    signOff: (sub, must) => write(`signOff ${sub} ${must}`, sub, () => { person(sub).sign_off = must ? "required" : null; }),
    revoke: (sub) => write(`revoke ${sub}`, sub, () => { state.people = state.people.filter((p) => p.sub !== sub); }),
    revokeScope: (scope) => write(`revokeScope ${scope.kind}:${scope.id}`, scope.id, () => { state.scopes = state.scopes.filter((s) => s.id !== scope.id); }),
    shareScope: (scope) => write(`shareScope ${scope.kind}:${scope.id}`, scope.id, () => { state.shared = true; state.scopes = [...state.scopes, scope]; }),
    stopAll: () => write("stopAll", "all", () => { state.people = []; state.scopes = []; }),
    // What the daemon is still sending, from this Mac: none unless a test queues some.
    invites: async () => ({ people: structuredClone(queue) }),
    ack: async (receipts) => {
      acks.push(...receipts);
      for (const q of queue) if (q.receipt && receipts.includes(q.receipt)) q.receipt = null;
      return { acknowledged: receipts.length };
    },
    dismiss: (sub) => write(`dismiss ${sub}`, sub, () => { queue.splice(queue.findIndex((q) => q.sub === sub), 1); }),
  };
  const queue: ShareQueued[] = [];
  const acks: string[] = [];
  return { state, calls, target, refuse, plan, held, queue, acks, set hold(on: boolean) { hold = on; } };
}

function Reader({ lib, directory = [], onStopAll }: { lib: ReturnType<typeof library>; directory?: LibraryPerson[]; onStopAll?: () => void }) {
  const [plan, setPlan] = useState(lib.plan());
  const [visible, show] = useState(true);
  return <><button onClick={() => show(true)}>Open</button><button onClick={() => setPlan(lib.plan())}>Plan changed</button><SharePanel plan={plan} target={lib.target} nameOf={(s) => s} onBusy={() => {}} owned onStopAll={onStopAll}
    teams={teams} directory={directory} writes={{ begin: () => true, end: (p) => setPlan(p ?? lib.plan()) }} visible={visible} onClose={() => show(false)} /></>;
}
const row = (sub: string) => screen.getByTestId(`share-person-${sub}`);
const choose = async (sub: string, item: string) => {
  fireEvent.click(within(row(sub)).getByRole("button", { name: `What ${sub} can do` }));
  await act(async () => fireEvent.click(within(screen.getByTestId(`share-menu-${sub}`)).getByRole(item === "Stop sharing" ? "menuitem" : item === "Must sign off" ? "menuitemcheckbox" : "menuitemradio", { name: new RegExp(`^${item}`) })));
};

it("lists each person's access and sign-off, teams and the org as rows, with display names that fit", async () => {
  const named = { ...people[0], id: "63a34f9d", display_name: `David ${"Longsurname".repeat(12)} Jones` };
  const lib = library({ scopes: [{ kind: "group", id: "platform" }, { kind: "org", id: "o1" }], people: [
    { sub: named.id, name: named.display_name, access: "edit", sign_off: "required" },
    { sub: "lena", name: null, access: "comment", sign_off: "required" },
    { sub: "sam", name: null, access: "view", sign_off: null },
  ] }, [named.id]);
  await act(async () => render(<Reader lib={lib} />));
  const david = row(named.id);
  expect([david.dataset.access, david.dataset.signOff, david.dataset.state]).toEqual(["edit", "required", "signed_off"]);
  expect(david.querySelector(".av")?.textContent).toBe("DJ");
  expect(david.querySelector(".nm")?.getAttribute("title")).toBe(named.display_name);
  expect([row("lena").dataset.access, row("lena").dataset.signOff, row("lena").dataset.state]).toEqual(["comment", "required", "waiting"]);
  expect([row("sam").dataset.access, row("sam").dataset.signOff]).toEqual(["view", "none"]);
  expect(screen.getByTestId("share-owner")).toBeTruthy();
  for (const scope of ["group-platform", "org-o1"]) {
    const team = screen.getByTestId(`share-scope-${scope}`);
    expect(within(team).getByRole("button", { name: /can do$/ }).textContent).toBe("Can view");
  }
  expect(screen.getByTestId("share-scope-group-platform").querySelector(".nm")?.textContent).toBe("Platform");
  // The search still shows who has access, and offers the rest.
  await act(async () => fireEvent.focus(screen.getByTestId("share-search")));
  expect(screen.getByTestId("share-result-sam").className).toContain("dim");
});

it("adds a person at the access chosen, and changes sign-off without touching their access", async () => {
  const lib = library({ people: [{ sub: "sam", name: null, access: "comment", sign_off: null }] });
  await act(async () => render(<Reader lib={lib} />));
  await act(async () => fireEvent.focus(screen.getByTestId("share-search")));
  fireEvent.click(within(screen.getByTestId("share-result-lena")).getByRole("button"));
  const add = screen.getByTestId("share-add");
  fireEvent.click(within(add).getByRole("menuitemradio", { name: /^Edit/ }));
  fireEvent.click(within(add).getByRole("menuitemcheckbox", { name: /^Must sign off/ }));
  await act(async () => fireEvent.click(screen.getByTestId("share-add-confirm")));
  expect(lib.calls).toEqual(["invite lena edit true"]);
  expect([row("lena").dataset.access, row("lena").dataset.signOff]).toEqual(["edit", "required"]);
  // Sign-off off: still an editor. Access down to view: the sign-off goes along.
  await choose("lena", "Must sign off");
  await choose("sam", "Must sign off");
  await choose("lena", "View");
  expect(lib.calls.slice(1)).toEqual(["signOff lena false", "signOff sam true", "change lena view"]);
  expect([row("lena").dataset.access, row("lena").dataset.signOff]).toEqual(["view", "none"]);
  expect([row("sam").dataset.access, row("sam").dataset.signOff]).toEqual(["comment", "required"]);
  expect(screen.getByText("The build starts when sam signs off.")).toBeTruthy();
});

it("keeps other people's changes when one fails, and sends only that one again", async () => {
  const lib = library();
  lib.hold = true;
  await act(async () => render(<Reader lib={lib} />));
  for (const sub of ["sam", "lena"]) {
    await act(async () => fireEvent.focus(screen.getByTestId("share-search")));
    fireEvent.click(within(screen.getByTestId(`share-result-${sub}`)).getByRole("button"));
    await act(async () => fireEvent.click(screen.getByTestId("share-add-confirm")));
  }
  expect(row("sam").dataset.state).toBe("saving");
  expect(row("lena").dataset.state).toBe("saving");
  // Adding the same person again while their write is out starts no second write.
  await act(async () => fireEvent.focus(screen.getByTestId("share-search")));
  fireEvent.click(within(screen.getByTestId("share-result-sam")).getByRole("button"));
  lib.refuse.add("lena");
  fireEvent.click(screen.getByRole("button", { name: "Close share" }));
  await act(async () => lib.held.shift()!());
  await act(async () => lib.held.shift()!());
  expect(lib.held).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "Open" }));
  expect(row("sam").dataset.state).toBe("waiting");
  expect(row("lena").dataset.state).toBe("failed");
  expect(lib.state.people.map((p) => p.sub)).toEqual(["sam"]);
  await act(async () => fireEvent.click(within(row("lena")).getByRole("button", { name: "Try again" })));
  await act(async () => lib.held.shift()!());
  expect(row("lena").dataset.state).toBe("waiting");
  expect(lib.calls).toEqual(["invite sam comment false", "invite lena comment false", "invite lena comment false"]);
  // A refused change leaves what the Library has.
  lib.refuse.add("sam");
  await choose("sam", "Edit");
  await act(async () => lib.held.shift()!());
  expect([row("sam").dataset.access, row("sam").dataset.state]).toEqual(["comment", "failed"]);
});

it("stops sharing with one person, one team, or everyone", async () => {
  const lib = library({ scopes: [{ kind: "group", id: "platform" }, { kind: "org", id: "o1" }], people: [
    { sub: "sam", name: null, access: "edit", sign_off: null }, { sub: "lena", name: null, access: "view", sign_off: "required" }] });
  let linkReads = 0;
  await act(async () => render(<Reader lib={lib} onStopAll={() => linkReads++} />));
  await choose("sam", "Stop sharing");
  expect(screen.queryByTestId("share-person-sam")).toBeNull();
  fireEvent.click(within(screen.getByTestId("share-scope-group-platform")).getByRole("button", { name: /can do$/ }));
  await act(async () => fireEvent.click(within(screen.getByTestId("share-menu-group-platform")).getByRole("menuitem", { name: "Stop sharing" })));
  expect(screen.queryByTestId("share-scope-group-platform")).toBeNull();
  expect(screen.getByTestId("share-scope-org-o1")).toBeTruthy();
  // Refused: everyone keeps it, the link section isn't told it stopped, and Try again sends it again.
  lib.refuse.add("all");
  fireEvent.click(screen.getByTestId("share-stop-all"));
  await act(async () => fireEvent.click(within(screen.getByTestId("share-stop-confirm")).getByRole("button", { name: "Stop sharing" })));
  expect(row("lena")).toBeTruthy();
  expect(linkReads).toBe(0);
  await act(async () => fireEvent.click(screen.getByTestId("share-try-all")));
  expect(linkReads).toBe(1);
  expect(lib.calls).toEqual(["revoke sam", "revokeScope group:platform", "stopAll", "stopAll"]);
  expect(screen.queryByTestId("share-person-lena")).toBeNull();
  expect(screen.queryByTestId("share-scope-org-o1")).toBeNull();
});

it("shares a plan nobody has yet from the same panel, saying it uploads a plan kept on this Mac", async () => {
  const lib = library({ shared: false, kept_here: true });
  const directory = [{ sub: "lena", email: "lena@example.com", display_name: "Lena Ortiz" }];
  await act(async () => render(<Reader lib={lib} directory={directory} />));
  expect(screen.getByTestId("share-upload").textContent).toContain("kept on this Mac");
  expect(screen.getByTestId("share-team-group-platform")).toBeTruthy();
  await act(async () => fireEvent.change(screen.getByTestId("share-search"), { target: { value: "ort" } }));
  fireEvent.click(within(screen.getByTestId("share-result-lena")).getByRole("button"));
  expect(within(screen.getByTestId("share-add")).getByText(/uploaded from this Mac/)).toBeTruthy();
  // The upload is refused twice: the row stays with the Library's error, Try again sends it again, and Cancel drops it.
  lib.refuse.add("lena");
  await act(async () => fireEvent.click(screen.getByTestId("share-add-confirm")));
  expect(row("lena").dataset.state).toBe("failed");
  expect(within(row("lena")).getByRole("alert").textContent).toBe("The Library refused invite lena comment false.");
  lib.refuse.add("lena");
  await act(async () => fireEvent.click(within(row("lena")).getByRole("button", { name: "Try again" })));
  expect(row("lena").dataset.state).toBe("failed");
  fireEvent.click(within(row("lena")).getByRole("button", { name: "Cancel" }));
  expect(screen.queryByTestId("share-person-lena")).toBeNull();
  expect(screen.getByTestId("share-upload")).toBeTruthy();
  await act(async () => fireEvent.change(screen.getByTestId("share-search"), { target: { value: "ort" } }));
  fireEvent.click(within(screen.getByTestId("share-result-lena")).getByRole("button"));
  await act(async () => fireEvent.click(screen.getByTestId("share-add-confirm")));
  expect(lib.calls).toEqual(Array(3).fill("invite lena comment false"));
  expect(row("lena").dataset.access).toBe("comment");
  expect(screen.queryByTestId("share-upload")).toBeNull();
});

it("shows an add from the daemon's answer while the Library can't be read, after reopening too, until it lands", async () => {
  const lib = library();
  const sends = lib.target.invite;
  const read = lib.target.access;
  let down = true;
  lib.target.access = async () => {
    if (down) throw new DaemonError("busy", "Can't reach your Library.", 503);
    return read();
  };
  // The daemon answers at once with the row it will send, and keeps it on this Mac until it lands.
  lib.target.invite = async (sub, access, signOff) => {
    lib.calls.push(`invite ${sub} ${access} ${signOff}`);
    const q: ShareQueued = { sub, name: null, access, sign_off: signOff ? "required" : null, op: "invite", state: "sharing", error: null, receipt: `r-${sub}` };
    lib.queue.push(structuredClone(q));
    return { ...lib.plan(), invite: q };
  };
  // This Mac's list of what is still being sent doesn't answer until the add has shown.
  const listed = lib.target.invites;
  const reads: (() => void)[] = [];
  let holdReads = true;
  lib.target.invites = () => holdReads ? new Promise((resolve) => reads.push(() => resolve(listed()))) : listed();
  await act(async () => render(<Reader lib={lib} />));
  await act(async () => fireEvent.focus(screen.getByTestId("share-search")));
  fireEvent.click(within(screen.getByTestId("share-result-lena")).getByRole("button"));
  await act(async () => fireEvent.click(screen.getByTestId("share-add-confirm")));
  expect(row("lena").dataset.state).toBe("sharing");                     // from the add's own answer
  expect(lib.acks).toEqual(["r-lena"]);                                   // whose receipt goes back, so it is sent
  expect(within(row("lena")).queryByRole("button", { name: "What lena can do" })).toBeNull();     // nothing to change until it lands
  holdReads = false;
  await act(async () => reads.forEach((go) => go()));
  fireEvent.click(screen.getByRole("button", { name: "Close share" }));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open" })));
  expect(row("lena").dataset.state).toBe("sharing");

  // It lands: the daemon lets it go, the Library lists her, and the plan changes.
  down = false;
  lib.queue.length = 0;
  lib.state.people.push({ sub: "lena", name: null, access: "comment", sign_off: null });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Plan changed" })));
  expect(row("lena").dataset.state).toBe("waiting");

  // One the Library refused says why, and Try again adds them again.
  lib.queue.push({ sub: "sam", name: null, access: "view", sign_off: null, op: "invite", state: "failed", error: "The Library refused it.", receipt: null });
  fireEvent.click(screen.getByRole("button", { name: "Close share" }));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open" })));
  expect(row("sam").dataset.state).toBe("failed");
  expect(within(row("sam")).getByRole("alert").textContent).toBe("The Library refused it.");
  lib.queue.length = 0;
  lib.target.invite = sends;
  await act(async () => fireEvent.click(within(row("sam")).getByRole("button", { name: "Try again" })));
  expect([row("sam").dataset.state, row("sam").dataset.access]).toEqual(["waiting", "view"]);
  expect(lib.calls).toEqual(["invite lena comment false", "invite sam view false"]);
});

it("never sends a change by itself that wasn't confirmed, and lets Send try a failed receipt again", async () => {
  // Sam's add was cancelled before its answer arrived: this Mac still has it, unconfirmed.
  const lib = library();
  lib.queue.push({ sub: "sam", name: null, access: "view", sign_off: null, op: "invite", state: "sharing", error: null, receipt: "r-sam" });
  const ack = lib.target.ack;
  let refuse = true;
  lib.target.ack = (receipts) => refuse ? Promise.reject(new Error("offline")) : ack(receipts);
  await act(async () => render(<Reader lib={lib} />));
  expect(row("sam").dataset.state).toBe("unconfirmed");
  expect(lib.acks).toEqual([]);                                           // shown, not sent
  await act(async () => fireEvent.click(within(row("sam")).getByRole("button", { name: "Send" })));
  expect(row("sam").dataset.state).toBe("unconfirmed");                  // its receipt didn't get through: still the person's
  refuse = false;
  await act(async () => fireEvent.click(within(row("sam")).getByRole("button", { name: "Send" })));
  expect(lib.acks).toEqual(["r-sam"]);
  expect(row("sam").dataset.state).toBe("sharing");
});

it("tries a refused removal again as a removal, never as an add", async () => {
  const lib = library({ people: [{ sub: "sam", name: null, access: "edit", sign_off: null }] });
  lib.queue.push({ sub: "sam", name: null, access: "edit", sign_off: null, op: "remove", state: "failed", error: "The Library refused it.", receipt: null });
  const revoke = lib.target.revoke;
  lib.target.revoke = (sub) => { lib.queue.length = 0; return revoke(sub); };
  await act(async () => render(<Reader lib={lib} />));
  expect([row("sam").dataset.state, row("sam").dataset.access]).toEqual(["failed", "edit"]);      // they still have it
  await act(async () => fireEvent.click(within(row("sam")).getByRole("button", { name: "Try again" })));
  expect(lib.calls).toEqual(["revoke sam"]);
  expect(screen.queryByTestId("share-person-sam")).toBeNull();
});

it("a team is shared from the first panel and then listed as a row", async () => {
  const lib = library({ shared: false, kept_here: false });
  await act(async () => render(<Reader lib={lib} />));
  expect(screen.getByTestId("share-upload").textContent).toContain("Library on brindle servers");
  await act(async () => fireEvent.click(within(screen.getByTestId("share-team-group-platform")).getByRole("button", { name: "Share" })));
  expect(lib.calls).toEqual(["shareScope group:platform"]);
  expect(screen.getByTestId("share-scope-group-platform")).toBeTruthy();
  expect(screen.queryByTestId("share-team-group-platform")).toBeNull();
});

it("discards a stale search", async () => {
  const replies: ((people: SharePerson[]) => void)[] = [];
  const lib = library();
  const target: SharePeopleTarget = { ...lib.target, search: (q) => q.q ? new Promise((resolve) => replies.push((rows) => resolve({ people: rows }))) : Promise.resolve({ people }) };
  await act(async () => render(<SharePanel plan={empty} target={target} writes={{ begin: () => true, end: () => {} }}
    nameOf={(s) => s} onBusy={() => {}} visible onClose={() => {}} />));
  const input = screen.getByTestId("share-search");
  fireEvent.change(input, { target: { value: "old" } });
  fireEvent.change(input, { target: { value: "new" } });
  await act(async () => replies[1]([people[1]]));
  await act(async () => replies[0]([people[0]]));
  expect(screen.queryByTestId("share-result-sam")).toBeNull();
  expect(screen.getByTestId("share-result-lena")).toBeTruthy();
});

it("says why a search failed", async () => {
  const lib = library();
  const target: SharePeopleTarget = { ...lib.target, search: () => Promise.reject(new Error("the daemon's reason")) };
  await act(async () => render(<SharePanel plan={empty} target={target} writes={{ begin: () => true, end: () => {} }}
    nameOf={(s) => s} onBusy={() => {}} visible onClose={() => {}} />));
  await act(async () => fireEvent.focus(screen.getByTestId("share-search")));
  expect(screen.getByTestId("share-search-error").textContent).toContain("the daemon's reason");
});

it("can reload the access list when the sign-in refresh is busy", async () => {
  const lib = library({ people: [{ sub: "sam", name: "Sam", access: "comment", sign_off: null }] });
  const read = lib.target.access;
  let blocked = true;
  lib.target.access = async () => {
    if (blocked) throw new DaemonError("busy", "Sign-in refresh in progress", 503);
    return read();
  };
  await act(async () => render(<Reader lib={lib} />));
  const access = within(screen.getByTestId("share-access"));
  const alert = access.getByRole("alert");
  blocked = false;
  await act(async () => fireEvent.click(within(alert).getByRole("button", { name: "Try again" })));
  expect(access.queryByRole("alert")).toBeNull();
  expect(row("sam").getAttribute("data-access")).toBe("comment");
});

it("updates link view and edit access while retaining the URL, and rolls back a refused change", async () => {
  let stored: DesignShareLink = { available: true, link: { url: "https://plans.pawdelta.com/b/dsl_one", access: "anyone", permission: "view" } };
  let refuse = false;
  const target: ShareLinkTarget = { key: "p1", read: async () => stored, copy: async () => stored, revoke: async () => ({ available: true, link: null }),
    update: async ({ permission }) => {
      if (refuse) throw new Error("forbidden");
      stored = { ...stored, link: { ...stored.link!, permission } };
      return stored;
    } };
  function Link() {
    const [state, setState] = useState(stored);
    return <ShareLinkPanel target={target} state={state} onState={setState} />;
  }
  render(<Link />);
  const toggle = screen.getByTestId("share-link-access") as HTMLSelectElement;
  for (const permission of ["edit", "view"]) {
    await act(async () => fireEvent.change(toggle, { target: { value: permission } }));
    expect(stored.link!.permission).toBe(permission);
    expect(toggle.value).toBe(permission);
    expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe("https://plans.pawdelta.com/b/dsl_one");
  }
  refuse = true;
  await act(async () => fireEvent.change(toggle, { target: { value: "edit" } }));
  expect(screen.getByTestId("share-link").dataset.state).toBe("error");
  expect(toggle.value).toBe("view");
});
