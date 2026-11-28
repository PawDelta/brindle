import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { SharedDesign } from "../../../api/client";
import { SharedDesignReader } from "./SharedDesignReader";

afterEach(cleanup);

async function open(state: "waiting" | "refused") {
  const doc = {
    id: "shared", session: "", title: "Workers", rev: 1, state: "open", markdown: "Where?", lede: "",
    sections: [{ id: "s", title: "Summary", markdown: "Where?" }],
    decisions: [{ id: "where", title: "Where?", options: [{ id: "local", label: "Here" }] }],
    threads: [{ id: 1, kind: "comment", rev: 1, author: "person", text: "Why?", status: "open",
      at: 1, anchor: { kind: "section", section: "s" }, replies: [] }],
    split: [], queued: 0, created_at: 1, me: "me", can_comment: true,
    outbox: [
      { client_id: "vote", tail: "/votes", state, body: { rev: 1, decision: "where", option: "local" } },
      { client_id: "reply", tail: "/comments", state, body: { parent: 1, text: "For privacy." } },
    ],
  } satisfies SharedDesign;
  let current: SharedDesign = doc;
  const client = { sharedDesign: async () => current, writeSharedDesign: async () => doc,
    libraryPeople: async () => ({ people: [] }) };
  await act(async () => { render(<SharedDesignReader client={client} docId={doc.id} onBack={() => {}} />); });
  async function refresh(changes: Partial<SharedDesign>) {
    current = { ...current, ...changes };
    await act(async () => { window.dispatchEvent(new Event("online")); });
  }
  return { card: within(screen.getByRole("radiogroup", { name: "Where?" })), doc, refresh };
}

it("keeps the current pick and both earlier refusal receipts after reconnect", async () => {
  const { card, doc, refresh } = await open("waiting");
  await refresh({ rev: 2, decisions: [{ ...doc.decisions[0], confirmed: "local" }],
    outbox: [{ ...doc.outbox[0], state: "refused" }, { client_id: "sign", tail: "/sign-offs", state: "refused", body: { rev: 1 } }] });
  expect(card.getByRole("radio").getAttribute("aria-checked")).toBe("true");
  expect(screen.getAllByText("Not sent")).toHaveLength(2);
});

it("keeps a queued pick selected and dims a queued reply", async () => {
  const { card, refresh } = await open("waiting");
  expect(card.getByRole("radio").getAttribute("aria-checked")).toBe("true");
  expect(card.getByText("Waiting to send")).toBeTruthy();
  const reply = screen.getAllByText("For privacy.")[0].closest('[data-state="waiting"]');
  expect(reply?.classList.contains("text-ink-3")).toBe(true);
  await refresh({ outbox: [{ client_id: "reply", tail: "/comments", state: "sent", body: { rev: 1, parent: 1 } }] });
  expect(screen.queryByText("For privacy.")).toBeNull();
  const receipt = screen.getByTestId("shared-design-reader").querySelector('[data-client-id="reply"] [data-state="sent"]');
  expect(receipt).toBeTruthy();
  // React selects the WebKit event in jsdom, which exposes WebkitAnimation without AnimationEvent.
  const end = "AnimationEvent" in window ? "animationend" : "webkitAnimationEnd";
  fireEvent(receipt!, new Event(end, { bubbles: true }));
  expect(receipt?.isConnected).toBe(false);
});

it("opens the refused pick on its card with its previous choice marked", async () => {
  const { card } = await open("refused");
  expect(card.getByRole("radio").getAttribute("aria-checked")).toBe("false");
  expect(card.getByText("Not sent")).toBeTruthy();
  expect(card.getByRole("radio").querySelector(".border-dashed")).toBeTruthy();
  fireEvent.click(card.getByRole("button", { name: "See what changed" }));
  expect(screen.getByRole("tab", { name: /History/ }).getAttribute("aria-selected")).toBe("true");
});
