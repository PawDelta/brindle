import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { LoroDoc } from "loro-crdt";
import { afterEach, expect, it } from "vitest";
import { fromBase64, type SharedDesign } from "../../../api/client";
import { FakeDaemon } from "../../../test/fakeDaemon";
import { DocEditor } from "./DocEditor";
import { DocPresence } from "./DocPresence";
import { SharedDesignReader } from "./SharedDesignReader";

afterEach(cleanup);

const settle = async () => {
  for (let i = 0; i < 10; i++) await act(async () => {});
};
const editor = (label: string) => EditorView.findFromDOM(screen.getByLabelText(label).closest<HTMLElement>(".cm-editor")!)!;

it("requires the author to freeze a teammate's document", async () => {
  const daemon = new FakeDaemon();
  daemon.seedLive("doc", { lede: "Text" }, { canFreeze: false });
  render(<DocPresence client={daemon.client()} visual="doc" isAuthor={false} name="Bo" />);
  await settle();
  const panel = screen.getByRole("region", { name: "Live document" });
  expect(panel.querySelector('[role="status"][data-state="author-required"]')).toBeTruthy();
  expect(within(panel).queryByRole("button")).toBeNull();
});

it("shows awareness colours, freezes credited text for everyone and withdraws sign-offs on reopen", async () => {
  const daemon = new FakeDaemon();
  daemon.seedLive("doc", { lede: "Text" }, { author: "Ann" });
  const client = daemon.client();
  render(<>
    <DocPresence client={client} visual="doc" isAuthor name="Ann" />
    <DocEditor client={client} visual="doc" section="lede" name="Ann" label="Ann's text" isAuthor />
    <DocEditor client={client} visual="doc" section="lede" name="Bo" label="Bo's text" />
  </>);
  await settle();
  const peers = screen.getByRole("list", { name: "Others in this document" });
  expect(within(peers).getAllByRole("listitem")).toHaveLength(1);
  expect(peers.querySelector('[data-peer="Bo"] [data-colour^="loro-peer-"]')).toBeTruthy();
  const ann = editor("Ann's text");
  const bo = editor("Bo's text");
  await act(async () => ann.dispatch({ changes: { from: 0, insert: "New " } }));
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "Freeze" }));
  await settle();
  expect(screen.getByRole("region", { name: "Live document" }).dataset.state).toBe("sign_off");
  expect(within(screen.getByRole("list", { name: "Credited writers" })).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["Ann"]);
  const frozen = daemon.liveState("doc").version!;
  const snapshot = new LoroDoc();
  snapshot.import(fromBase64(frozen.snapshot));
  expect(snapshot.getText("lede").toString()).toBe("New Text");
  render(<DocEditor client={client} visual="doc" section="lede" name="Cy" label="Cy's text" />);
  await settle();
  expect(document.querySelector('[contenteditable="true"]')).toBeNull();
  for (const view of [ann, bo, editor("Cy's text")]) {
    expect(view.state.facet(EditorState.readOnly)).toBe(true);
    await act(async () => view.dispatch({ changes: { from: 0, insert: "Blocked " } }));
    expect(view.state.doc.toString()).toBe("New Text");
  }
  await act(async () => daemon.setLiveState("doc", { version: { ...frozen, signOffs: ["Bo"] } }));
  fireEvent.click(screen.getByRole("button", { name: "Reopen" }));
  await settle();
  expect(daemon.liveState("doc").version).toBeNull();
  expect(screen.queryByTestId("doc-version")).toBeNull();
  expect(ann.state.facet(EditorState.readOnly)).toBe(false);
  await act(async () => bo.dispatch({ changes: { from: 0, insert: "Again " } }));
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "Freeze" }));
  await settle();
  expect(daemon.liveState("doc").version?.id).toBeGreaterThan(frozen.id);
  expect(daemon.liveState("doc").version?.signOffs).toEqual([]);
  expect(daemon.liveState("doc").version?.writers).toContain("Bo");
});

it("locks others' text while the author is offline and still sends comments and suggestions", async () => {
  const daemon = new FakeDaemon();
  daemon.seedLive("doc", { lede: "Workers run here." }, { author: "Ann", canFreeze: false });
  const live = daemon.client();
  const design: SharedDesign = { id: "doc", session: "", title: "Workers", rev: 1, state: "open", markdown: "Workers run here.", lede: "Workers run here.", sections: [], decisions: [], split: [], queued: 0, created_at: 1, can_comment: true, can_suggest: true, author: "Ann", me: "Bo" };
  const sent: string[] = [];
  const client = {
    visualLive: live.visualLive.bind(live),
    libraryPeople: async () => ({ people: [] }),
    sharedDesign: async () => design,
    writeSharedDesign: async (_id: string, tail: string) => { sent.push(tail); return design; },
  };
  render(<>
    <SharedDesignReader client={client} docId="doc" onBack={() => {}} />
    <DocEditor client={live} visual="doc" section="lede" name="Bo" label="Bo's text" />
  </>);
  await settle();
  await act(async () => daemon.setLiveState("doc", { lock: "author_offline" }));
  const panel = screen.getByRole("region", { name: "Live document" });
  expect(panel.dataset.state).toBe("author_offline");
  expect(within(panel).getAllByRole("status").length).toBeGreaterThan(0);
  const bo = editor("Bo's text");
  expect(bo.state.facet(EditorState.readOnly)).toBe(true);
  expect(document.querySelector('[contenteditable="true"]')).toBeNull();
  await act(async () => bo.dispatch({ changes: { from: 0, insert: "Blocked " } }));
  expect(daemon.liveText("doc", "lede")).toBe("Workers run here.");
  const highlight = () => {
    const text = within(screen.getByTestId("plan-doc")).getByText("Workers run here.").firstChild!;
    const range = document.createRange();
    range.setStart(text, 0);
    range.setEnd(text, 7);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);
    fireEvent.mouseUp(screen.getByTestId("plan-doc"));
  };
  highlight();
  fireEvent.click(within(screen.getByRole("toolbar")).getByRole("button", { name: "Comment" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Comment" }), { target: { value: "Where?" } });
  await act(async () => fireEvent.submit(screen.getByTestId("thread-draft")));
  highlight();
  fireEvent.click(within(screen.getByRole("toolbar")).getByRole("button", { name: "Suggest edit" }));
  fireEvent.change(screen.getByRole("textbox", { name: /Replace/ }), { target: { value: "Agents" } });
  await act(async () => fireEvent.submit(screen.getByTestId("thread-draft")));
  expect(sent).toEqual(["comments", "suggestions"]);
});
