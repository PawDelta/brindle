import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { DaemonError, type SharedDesign } from "../../../api/client";
import type { SharedDesignClient } from "./client";
import { SharedDesignReader } from "./SharedDesignReader";

afterEach(cleanup);

const design: SharedDesign = {
  id: "doc_1", session: "", title: "Cloud workers", rev: 1, state: "open", markdown: "Why.", lede: "Why.", sections: [{ id: "s", title: "Summary", markdown: "Workers run here." }, { id: "fig", title: "Flow", kind: "mermaid", markdown: "graph TD; A-->B" }],
  decisions: [], split: [], queued: 0, created_at: 1, me: "u_me", author: "u_author", can_comment: true, can_suggest: true, can_sign_off: true, signed_off: false,
  gate: { state: "waiting", required: 1, signed: 0, waiting_on: ["u_me"], reviewers: [{ sub: "u_me", required: true, status: "waiting", you: true }] },
};

it("a teammate signs off through the Library, keeps a draft across a stale write, and loses the doc with their access", async () => {
  let doc = design;
  let reading: () => Promise<SharedDesign> = async () => doc;
  const sent: [string, Record<string, unknown>][] = [];
  let answer: (tail: string) => Promise<SharedDesign> = async () => doc;
  const client: SharedDesignClient = {
    sharedDesign: () => reading(),
    writeSharedDesign: (_doc, tail, body) => { sent.push([tail, body]); return answer(tail); },
    libraryPeople: async () => ({ people: [] }),
  };
  await act(async () => { render(<SharedDesignReader client={client} docId="doc_1" onBack={() => {}} />); });
  expect(screen.getByText("Workers run here.")).toBeTruthy();
  expect(screen.queryByRole("radio", { name: "Editing" })).toBeNull();

  // A comment that meets a moved revision: read again, and what was written is still there.
  await act(async () => fireEvent.click(within(screen.getByTestId("plan-doc")).getByRole("button", { name: "Comment" })));
  const box = () => screen.getByRole("textbox", { name: "Comment" });
  fireEvent.change(box(), { target: { value: "Which VM?" } });
  answer = async () => { doc = { ...design, rev: 2 }; throw new DaemonError("stale", "stale", 409); };
  await act(async () => fireEvent.click(within(screen.getByTestId("thread-draft")).getByRole("button", { name: "Comment" })));
  expect(screen.getByRole("alert").textContent).toContain("changed");
  expect((box() as HTMLTextAreaElement).value).toBe("Which VM?");
  expect(screen.getByTestId("plan-rev").textContent).toBe("rev 2");

  answer = async () => (doc = { ...doc, signed_off: true });
  await act(async () => fireEvent.click(screen.getByTestId("plan-sign-off")));
  expect(sent.at(-1)).toEqual(["sign-offs", { rev: 2 }]);
  expect(screen.queryByTestId("plan-sign-off")).toBeNull();

  // A refusal on the next read takes the doc off the screen.
  reading = async () => { throw new DaemonError("forbidden", "no access", 403); };
  answer = reading;
  fireEvent.change(box(), { target: { value: "Which VM? Any." } });
  await act(async () => fireEvent.click(within(screen.getByTestId("thread-draft")).getByRole("button", { name: "Comment" })));
  expect(screen.queryByText("Workers run here.")).toBeNull();
  expect(screen.getByRole("alert").textContent).toContain("no access");
});
