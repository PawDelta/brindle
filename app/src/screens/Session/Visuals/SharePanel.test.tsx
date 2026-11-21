import { useState } from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { DesignShareLink, Plan, SharePerson } from "../../../api/client";
import { SharePanel, type SharePeopleTarget } from "./SharePanel";
import { ShareLinkPanel, type ShareLinkTarget } from "./ShareLink";

afterEach(cleanup);

const people: SharePerson[] = ["sam", "lena"].map((id) => ({ id, display_name: id, email: `${id}@example.com`,
  teams: [{ id: "platform", name: "Platform" }], role: "member", on_plan: false, on_plan_role: null }));
const empty: Plan = { id: "p1", rev: 1, title: "Design", state: "open", decisions: [], split: [], sections: [],
  session: "boss", created_at: 1, lede: "", markdown: "", queued: 0,
  gate: { state: "ready", required: 0, signed: 0, waiting_on: [], reviewers: [] } };

it("uses display names and email fallbacks for bounded people rows and retains sign-off state", async () => {
  const named = { ...people[0], id: "63a34f9d", display_name: `David ${"Longsurname".repeat(12)} Jones` };
  const emailOnly = { ...people[1], display_name: "", email: "lena.long@example.com" };
  const plan = { ...empty, gate: { ...empty.gate!, reviewers: [{ sub: named.id, required: true, status: "signed_off" as const }] } };
  const target = { search: async () => ({ people: [named, emailOnly] }), put: async () => plan, remove: async () => plan };
  await act(async () => render(<SharePanel plan={plan} target={target} nameOf={(s) => s} writes={{ begin: () => true, end: () => {} }} onBusy={() => {}} visible onClose={() => {}} />));
  const row = screen.getByTestId(`share-reviewer-${named.id}`);
  expect(row.dataset.state).toBe("signed_off");
  expect(row.querySelector(".av")?.textContent).toBe("DJ");
  expect(row.querySelector(".nm")?.getAttribute("title")).toBe(named.display_name);
  expect(row.querySelector(".nm")?.className).toContain("truncate");
  await act(async () => fireEvent.focus(screen.getByTestId("share-search")));
  const result = screen.getByTestId("share-result-lena");
  expect(result.querySelector(".av")?.textContent).toBe("L");
  expect(result.querySelector(".nm")?.getAttribute("title")).toContain(emailOnly.email);
});

it("keeps two invitations visible, isolates a failure, and sends that person again without replacing the other", async () => {
  let remote = empty;
  const complete: (() => void)[] = [];
  let fail = true;
  let refuseRoleChange = false;
  const target: SharePeopleTarget = {
    search: async () => ({ people }),
    put: (sub, required) => new Promise<Plan>((resolve, reject) => complete.push(() => {
      if (sub === "lena" && fail) { reject(new Error("conflict")); return; }
      if (sub === "sam" && !required && refuseRoleChange) { reject(new Error("conflict")); return; }
      remote = { ...remote, gate: { ...remote.gate!, reviewers: [...remote.gate!.reviewers!.filter((r) => r.sub !== sub), { sub, required, status: "waiting" }] } };
      resolve(remote);
    })),
    remove: async (sub) => (remote = { ...remote, gate: { ...remote.gate!, reviewers: remote.gate!.reviewers!.filter((r) => r.sub !== sub) } }),
  };
  function Reader() {
    const [plan, setPlan] = useState(empty);
    const [visible, show] = useState(true);
    return <><button onClick={() => show(true)}>Open</button><SharePanel plan={plan} target={target} nameOf={(s) => s} onBusy={() => {}}
      writes={{ begin: () => true, end: (p) => setPlan(p ?? remote) }} visible={visible} onClose={() => show(false)} /></>;
  }
  await act(async () => render(<Reader />));
  await act(async () => fireEvent.focus(screen.getByTestId("share-search")));
  fireEvent.click(screen.getByTestId("share-add-all"));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "As required" })));
  expect(screen.getByTestId("share-reviewer-sam").dataset.state).toBe("inviting");
  expect(screen.getByTestId("share-reviewer-lena").dataset.state).toBe("inviting");
  // Repeated selection while its request is out must not start another write.
  fireEvent.keyDown(screen.getByTestId("share-search"), { key: "Enter" });
  fireEvent.click(screen.getByRole("button", { name: "Close share" }));
  await act(async () => complete.shift()!());
  await act(async () => complete.shift()!());
  fireEvent.click(screen.getByRole("button", { name: "Open" }));
  expect(screen.getByTestId("share-reviewer-sam").dataset.state).toBe("invited");
  expect(screen.getByTestId("share-reviewer-lena").dataset.state).toBe("failed");
  expect(remote.gate!.reviewers!.map((r) => r.sub)).toEqual(["sam"]);
  fail = false;
  await act(async () => fireEvent.click(screen.getByTestId("share-try-lena")));
  await act(async () => complete.shift()!());
  expect(screen.getByTestId("share-reviewer-lena").dataset.state).toBe("invited");
  expect(remote.gate!.reviewers!.map((r) => r.sub)).toEqual(["sam", "lena"]);
  expect(complete).toHaveLength(0);
  fireEvent.click(within(screen.getByTestId("share-reviewer-lena")).getByRole("button"));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Make optional" })));
  await act(async () => complete.shift()!());
  expect(screen.getByTestId("share-reviewer-lena").dataset.required).toBe("false");
  fireEvent.click(within(screen.getByTestId("share-reviewer-sam")).getByRole("button"));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Remove from plan" })));
  expect(screen.queryByTestId("share-reviewer-sam")).toBeNull();
  expect(remote.gate!.reviewers!.map((r) => r.sub)).toEqual(["lena"]);
  expect((screen.getByTestId("share-search") as HTMLInputElement).value).toBe("");

  fireEvent.click(within(screen.getByTestId("share-reviewer-lena")).getByRole("button"));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Remove from plan" })));
  fireEvent.click(screen.getByTestId("share-add-all"));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "As required" })));
  await act(async () => complete.shift()!());
  // Sam's invite is confirmed while Lena's is still queued, before the reader receives new props.
  expect(screen.getByTestId("share-reviewer-lena").dataset.state).toBe("inviting");
  fireEvent.click(within(screen.getByTestId("share-reviewer-sam")).getByRole("button"));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Make optional" })));
  await act(async () => complete.shift()!());
  refuseRoleChange = true;
  await act(async () => complete.shift()!());
  expect(remote.gate!.reviewers!.find((r) => r.sub === "sam")!.required).toBe(true);
  expect(screen.getByTestId("share-reviewer-sam").dataset.required).toBe("true");
  expect(screen.getByTestId("share-reviewer-sam").dataset.state).toBe("failed");
  refuseRoleChange = false;
  await act(async () => fireEvent.click(screen.getByTestId("share-try-sam")));
  await act(async () => complete.shift()!());
  expect(remote.gate!.reviewers!.find((r) => r.sub === "sam")!.required).toBe(false);
  expect(screen.getByTestId("share-reviewer-sam").dataset.required).toBe("false");
});

it("discards stale searches and searches a pasted list under the selected team", async () => {
  const replies: ((people: SharePerson[]) => void)[] = [];
  const target: SharePeopleTarget = {
    search: async (q) => {
      if (!q.q) return { people };
      if (q.q.includes("@")) return { people: people.filter((p) => p.email === q.q && q.team === "platform") };
      return new Promise((resolve) => replies.push((rows) => resolve({ people: rows })));
    }, put: async () => empty, remove: async () => empty,
  };
  await act(async () => render(<SharePanel plan={empty} target={target} writes={{ begin: () => true, end: () => {} }}
    nameOf={(s) => s} onBusy={() => {}} visible onClose={() => {}} />));
  const input = screen.getByTestId("share-search");
  fireEvent.change(input, { target: { value: "old" } });
  fireEvent.change(input, { target: { value: "new" } });
  await act(async () => replies[1]([people[1]]));
  await act(async () => replies[0]([people[0]]));
  expect(screen.queryByTestId("share-result-sam")).toBeNull();
  expect(screen.getByTestId("share-result-lena")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Filter" }));
  await act(async () => fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Platform" })));
  await act(async () => fireEvent.paste(input, { clipboardData: { getData: () => "sam@example.com; lena@example.com; missing@example.com" } }));
  expect(screen.getByTestId("share-result-sam")).toBeTruthy();
  expect(screen.getByTestId("share-result-lena")).toBeTruthy();
  expect(screen.getByTestId("share-add-all")).toBeTruthy();
  expect(screen.queryByTestId("share-result-missing")).toBeNull();
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
