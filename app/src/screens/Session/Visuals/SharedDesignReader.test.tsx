import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DaemonError, type DesignShareLink, type SharedDesign } from "../../../api/client";
import type { ShareLinkClient, SharedDesignClient } from "./client";
import { SharedDesignReader } from "./SharedDesignReader";
import { AccessRequest } from "./AccessRequest";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const design: SharedDesign = {
  id: "doc_1", session: "", title: "Cloud workers", rev: 1, state: "open", markdown: "Why.", lede: "Why.", sections: [{ id: "s", title: "Summary", markdown: "Workers run here." }, { id: "fig", title: "Flow", kind: "mermaid", markdown: "graph TD; A-->B" }],
  decisions: [], split: [], queued: 0, created_at: 1, me: "u_me", author: "u_author", can_comment: true, can_suggest: true, can_sign_off: true, signed_off: false,
  gate: { state: "waiting", required: 1, signed: 0, waiting_on: ["u_me"], reviewers: [{ sub: "u_me", required: true, status: "waiting", you: true }] },
};

it.each([
  [false, false, "You can view"], [false, true, "You can comment"], [true, true, "You can edit"],
])("shows actual access for edit=%s comment=%s", async (can_edit, can_comment, line) => {
  const client: SharedDesignClient = {
    sharedDesign: async () => ({ ...design, can_edit, can_comment, can_suggest: false, can_sign_off: false }),
    writeSharedDesign: async () => design, libraryPeople: async () => ({ people: [] }),
  };
  await act(async () => { render(<SharedDesignReader client={client} docId="doc_1" onBack={() => {}} />); });
  expect(screen.getAllByText(line).length).toBeGreaterThanOrEqual(2);
  if (!can_comment) expect(screen.queryByRole("button", { name: "Comment" })).toBeNull();
});

it.each(["s", "context"])("asks the author for access and keeps a sent request disabled in section %s", async (section) => {
  const requests: string[] = [];
  const client: SharedDesignClient = {
    sharedDesign: async () => ({ ...design, sections: [{ id: section, title: "Context", markdown: "Context." }], can_comment: false, can_suggest: false, can_sign_off: false }),
    writeSharedDesign: async (_id, _tail, body) => { requests.push(String(body.role)); return { ...design, can_comment: false, access_request: { role: "comment", status: "pending" } }; },
    libraryPeople: async () => ({ people: [] }),
  };
  await act(async () => { render(<SharedDesignReader client={client} docId="doc_1" onBack={() => {}} />); });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Ask to comment" })));
  expect(requests).toEqual(["comment"]);
  expect(screen.getByRole("button", { name: "Comment access requested" }).hasAttribute("disabled")).toBe(true);
});

it("reconciles a sent access request with a later decline without remounting", async () => {
  const viewer = { ...design, can_comment: false };
  const client: SharedDesignClient = {
    sharedDesign: async () => viewer,
    writeSharedDesign: async () => ({ ...viewer, access_request: { role: "comment", status: "pending" } }),
    libraryPeople: async () => ({ people: [] }),
  };
  const { rerender } = render(<AccessRequest client={client} doc={viewer} />);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Ask to comment" })));
  expect(screen.getByRole("button", { name: "Comment access requested" }).hasAttribute("disabled")).toBe(true);
  rerender(<AccessRequest client={client} doc={{ ...viewer, access_request: { role: "comment", status: "pending" } }} />);
  expect(screen.getByRole("button", { name: "Comment access requested" }).hasAttribute("disabled")).toBe(true);
  rerender(<AccessRequest client={client} doc={{ ...viewer, access_request: { role: "comment", status: "declined" } }} />);
  expect(screen.getByRole("button", { name: "Ask to comment" }).hasAttribute("disabled")).toBe(false);
});

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
