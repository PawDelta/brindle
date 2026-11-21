import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DaemonError, type DesignShareLink, type SharedDesign } from "../../../api/client";
import type { ShareLinkClient, SharedDesignClient } from "./client";
import { SharedDesignReader } from "./SharedDesignReader";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const design: SharedDesign = {
  id: "doc_1", session: "", title: "Cloud workers", rev: 1, state: "open", markdown: "Why.", lede: "Why.", sections: [{ id: "s", title: "Summary", markdown: "Workers run here." }, { id: "fig", title: "Flow", kind: "mermaid", markdown: "graph TD; A-->B" }],
  decisions: [], split: [], queued: 0, created_at: 1, me: "u_me", author: "u_author", can_comment: true, can_suggest: true, can_sign_off: true, signed_off: false,
  gate: { state: "waiting", required: 1, signed: 0, waiting_on: ["u_me"], reviewers: [{ sub: "u_me", required: true, status: "waiting", you: true }] },
};

it("a teammate signs off through the Library, keeps a draft across a stale write, and loses the doc with their access", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  const nextRead = () => act(async () => { vi.advanceTimersByTime(30_000); });
  let doc: SharedDesign = { ...design, teammate_names: { u_david: "A teammate", u_reply: "A teammate" }, threads: [{ id: 71, kind: "comment", rev: 1, author: "u_david", author_name: "A teammate", text: "Clarify this.", at: 1, status: "open", anchor: { kind: "section", section: "s" },
    replies: [{ id: 72, author: "u_reply", author_name: "A teammate", text: "Agreed.", at: 2 }] }] };
  let reading: () => Promise<SharedDesign> = async () => doc;
  const sent: [string, Record<string, unknown>][] = [];
  let answer: (tail: string) => Promise<SharedDesign> = async () => doc;
  const client: SharedDesignClient = {
    sharedDesign: () => reading(),
    writeSharedDesign: (_doc, tail, body) => { sent.push([tail, body]); return answer(tail); },
    libraryPeople: async () => ({ people: [{ sub: "u_david", display_name: "David Chen", email: "david@example.test" }, { sub: "u_reply", email: "reply@example.test" }] }),
  };
  await act(async () => { render(<SharedDesignReader client={client} docId="doc_1" onBack={() => {}} />); });
  expect(screen.getByText("Workers run here.")).toBeTruthy();
  expect(screen.getAllByText("David Chen").length).toBeGreaterThan(0);
  expect(screen.getAllByText("reply@example.test").length).toBeGreaterThan(0);
  expect(screen.queryByRole("radio", { name: "Editing" })).toBeNull();

  // A comment that meets a moved revision: read again, and what was written is still there.
  await act(async () => fireEvent.click(within(screen.getByTestId("plan-doc")).getByRole("button", { name: "Comment" })));
  const box = () => screen.getByRole("textbox", { name: "Comment" });
  fireEvent.change(box(), { target: { value: "Which VM?" } });
  answer = async () => { doc = { ...design, rev: 2 }; throw new DaemonError("stale", "stale", 409); };
  await act(async () => fireEvent.click(within(screen.getByTestId("thread-draft")).getByRole("button", { name: "Comment" })));
  expect(screen.getByRole("alert")).toBeTruthy();
  expect((box() as HTMLTextAreaElement).value).toBe("Which VM?");
  expect(screen.getByTestId("plan-rev").textContent).toBe("rev 2");

  answer = async () => (doc = { ...doc, signed_off: true });
  await act(async () => fireEvent.click(screen.getByTestId("plan-sign-off")));
  expect(sent.at(-1)).toEqual(["sign-offs", { rev: 2 }]);
  expect(screen.queryByTestId("plan-sign-off")).toBeNull();

  // An outage says nothing about access: what was on screen stays.
  reading = async () => { throw new DaemonError("upstream", "offline", 502); };
  await nextRead();
  expect(screen.getByText("Workers run here.")).toBeTruthy();

  // A write goes out, then a read is refused: the doc leaves, and the write's late reply can't
  // bring it back while the read after it is still out. Only that read can.
  let reply!: (d: SharedDesign) => void;
  answer = () => new Promise<SharedDesign>((resolve) => { reply = resolve; });
  await act(async () => fireEvent.click(within(screen.getByTestId("thread-draft")).getByRole("button", { name: "Comment" })));
  reading = async () => { throw new DaemonError("forbidden", "no access", 403); };
  await nextRead();
  expect(screen.queryByText("Workers run here.")).toBeNull();
  expect(screen.getByRole("alert")).toBeTruthy();

  let granted!: (d: SharedDesign) => void;
  reading = () => new Promise<SharedDesign>((resolve) => { granted = resolve; });
  await act(async () => reply(doc));
  expect(screen.queryByText("Workers run here.")).toBeNull();
  await act(async () => granted(doc));
  expect(screen.getByText("Workers run here.")).toBeTruthy();
});

it("an editor on Pro copies the design's link, revokes it, and sees an org policy's refusal; Free gets no link", async () => {
  const url = "https://pawdelta.com/api/brindle/v1/share-links/dsl_x";
  let current: DesignShareLink = { available: true, link: null };
  const copied: string[] = [];
  Object.assign(navigator, { clipboard: { writeText: async (text: string) => { copied.push(text); } } });
  let copy: () => Promise<DesignShareLink> = async () => (current = { available: true, link: { url, created_at: 1, access: "anyone" } });
  const revoke = vi.fn(async () => (current = { available: true, link: null }));
  const client: SharedDesignClient & ShareLinkClient = {
    sharedDesign: async () => ({ ...design, can_edit: true }),
    writeSharedDesign: async () => design,
    libraryPeople: async () => ({ people: [] }),
    designShareLink: async () => current,
    copyDesignShareLink: () => copy(),
    revokeDesignShareLink: revoke,
  };
  const { unmount } = render(<SharedDesignReader client={client} docId="doc_1" onBack={() => {}} />);
  await act(async () => {});
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Share" })));
  const panel = () => within(screen.getByTestId("share-link"));
  await act(async () => fireEvent.click(panel().getByRole("button", { name: "Copy link" })));
  expect(copied).toEqual([url]);
  expect((panel().getByRole("textbox") as HTMLInputElement).value).toBe(url);

  await act(async () => fireEvent.click(panel().getByRole("button", { name: "Revoke link" })));
  expect(revoke).toHaveBeenCalledWith("doc_1", undefined);
  expect(panel().queryByRole("textbox")).toBeNull();

  copy = async () => { throw new DaemonError("share_links_off", "an organization that holds this design does not allow share links", 403); };
  await act(async () => fireEvent.click(panel().getByRole("button", { name: "Copy link" })));
  expect(panel().getByRole("alert").textContent).toContain("does not allow share links");
  unmount();

  current = { available: false, link: null };
  render(<SharedDesignReader client={client} docId="doc_1" onBack={() => {}} />);
  await act(async () => {});
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Share" })));   // its review is shared: reviewers only
  expect(screen.queryByTestId("share-link")).toBeNull();
});

it("a teammate adds an own answer beside the author's, and the card counts it as their pick", async () => {
  const theirs = { id: "own-author1", text: "only on federal holidays", author: "u_author", at: 1, locked: false };
  const decision = { id: "skip", title: "When should the cleanup skip a night?", section: "proposed-design", mine: "a",
    options: [{ id: "a", label: "A · Skip on weekends" }, { id: "b", label: "B · Never skip" }], answers: [theirs], votes: { "own-author1": ["u_author"] } };
  let doc: SharedDesign = { ...design, teammate_names: { u_author: "David" }, decisions: [decision],
    sections: [{ id: "context", title: "Context", markdown: "Nightly cleanup." }, { id: "proposed-design", title: "Proposed design", markdown: "It prunes worktrees." }] };
  const sent: [string, Record<string, unknown>][] = [];
  const client: SharedDesignClient = {
    sharedDesign: async () => doc,
    writeSharedDesign: async (_doc, tail, body) => {
      sent.push([tail, body]);
      const mine = { id: "own-me1", text: String(body.text), author: "person", at: 2, locked: false };
      doc = { ...doc, decisions: [{ ...decision, answers: [theirs, mine], votes: { ...decision.votes, "own-me1": ["person"] }, confirmed: "own-me1" }] };
      return doc;
    },
    libraryPeople: async () => ({ people: [] }),
  };
  await act(async () => { render(<SharedDesignReader client={client} docId="doc_1" onBack={() => {}} />); });
  expect(screen.getAllByTestId("plan-answer").map((a) => a.dataset.answer)).toEqual(["own-author1"]);

  await act(async () => fireEvent.click(screen.getByTestId("plan-add-answer")));
  fireEvent.change(screen.getByRole("textbox", { name: "Your own answer" }), { target: { value: "  when the repo is idle " } });
  await act(async () => fireEvent.submit(screen.getByTestId("plan-answer-field")));

  expect(sent).toEqual([["answers", { rev: 1, decision: "skip", text: "when the repo is idle" }]]);
  expect(screen.queryByTestId("plan-answer-field")).toBeNull();
  const rows = screen.getAllByTestId("plan-answer");
  expect(rows.map((a) => [a.dataset.answer, a.dataset.author, !!a.dataset.chosen])).toEqual([["own-author1", "u_author", false], ["own-me1", "person", true]]);
  expect(screen.getByTestId("plan-decision").dataset.state).toBe("decided");
  expect(within(rows[0]).queryByRole("button", { name: "Edit" })).toBeNull();     // only an author rewords their own
  expect(within(rows[1]).getByRole("button", { name: "Edit" })).toBeTruthy();
});
