import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { EditorView } from "@codemirror/view";
import { afterEach, expect, it, vi } from "vitest";
import { LIVE_RELOAD } from "../../../api/client";
import { FakeDaemon } from "../../../test/fakeDaemon";
import { DocEditor } from "./DocEditor";
import { FormatBar, FormatProvider } from "./DocFormat";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

/* The fake daemon delivers frames on microtasks: let them all land. */
const settle = async () => {
  for (let i = 0; i < 10; i++) await act(async () => {});
};

/* Two people with the same section open, each through their own client of the daemon. */
async function twoWriters(text: string) {
  const daemon = new FakeDaemon();
  daemon.seedLive("plan-1", { goals: text });
  render(
    <>
      <div data-testid="ann"><DocEditor client={daemon.client()} visual="plan-1" section="goals" name="Ann" label="Ann's text" /></div>
      <div data-testid="bo"><DocEditor client={daemon.client()} visual="plan-1" section="goals" name="Bo" label="Bo's text" /></div>
    </>,
  );
  await settle();
  const viewIn = (who: string) => EditorView.findFromDOM(screen.getByTestId(who).querySelector<HTMLElement>(".cm-editor")!)!;
  return { daemon, ann: viewIn("ann"), bo: viewIn("bo") };
}

it("brings two writers' concurrent edits to the same text, and keeps an idle writer present past the expiry", async () => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  const { daemon, ann, bo } = await twoWriters("Ship the editor.");
  // Both type before either hears of the other's change.
  ann.dispatch({ changes: { from: 0, insert: "First, " } });
  bo.dispatch({ changes: { from: bo.state.doc.length, insert: " Then test it." } });
  await settle();
  expect(ann.state.doc.toString()).toBe("First, Ship the editor. Then test it.");
  expect(bo.state.doc.toString()).toBe(ann.state.doc.toString());
  expect(daemon.liveText("plan-1", "goals")).toBe(ann.state.doc.toString());

  // Well past the 30 s expiry with nobody typing: someone joining now still learns both names.
  await act(async () => void vi.advanceTimersByTime(45_000));
  await settle();
  const names = Object.values(daemon.livePresence("plan-1", "goals")).map((v) => (v as { name?: string }).name).filter(Boolean);
  expect(names.sort()).toEqual(["Ann", "Bo"]);
});

it("waits read only for a number from the daemon, writes under it, and opens again when the text is replaced", async () => {
  const daemon = new FakeDaemon();
  daemon.seedLive("plan-1", { goals: "Ship." });
  daemon.holdLivePeers = true;
  render(<DocEditor client={daemon.client()} visual="plan-1" section="goals" name="Ann" label="Ann's text" />);
  await settle();
  const view = () => EditorView.findFromDOM(document.querySelector<HTMLElement>(".cm-editor")!)!;
  view().dispatch({ changes: { from: 0, insert: "Not yet: " } });
  await settle();
  expect(view().state.doc.toString()).toBe("Ship.");

  daemon.lendLivePeers();
  await settle();
  view().dispatch({ changes: { from: 0, insert: "Now: " } });
  await settle();
  expect(daemon.liveText("plan-1", "goals")).toBe("Now: Ship.");
  expect(daemon.liveRefused).toEqual([]);

  // The daemon took the server relay's draft instead: the editor loads it afresh.
  daemon.seedLive("plan-1", { goals: "Theirs." });
  daemon.liveSockets.at(-1)!.close(LIVE_RELOAD);
  await settle();
  expect(view().state.doc.toString()).toBe("Theirs.");
});

it("writes the formatting toolbar's Markdown into the live draft for every writer", async () => {
  const daemon = new FakeDaemon();
  daemon.seedLive("plan-1", { goals: "Ship the editor.\nTest it." });
  render(
    <FormatProvider>
      <FormatBar />
      <div data-testid="ann"><DocEditor client={daemon.client()} visual="plan-1" section="goals" name="Ann" label="Ann's text" /></div>
      <div data-testid="bo"><DocEditor client={daemon.client()} visual="plan-1" section="goals" name="Bo" label="Bo's text" variant="source" /></div>
    </FormatProvider>,
  );
  await settle();
  const viewIn = (who: string) => EditorView.findFromDOM(screen.getByTestId(who).querySelector<HTMLElement>(".cm-editor")!)!;
  const ann = viewIn("ann");
  // Bold over "Ship", then the second line made a bulleted item: both as one change each.
  ann.dispatch({ selection: { anchor: 0, head: 4 } });
  fireEvent.focus(ann.contentDOM);
  fireEvent.click(screen.getByRole("button", { name: "Bold" }));
  ann.dispatch({ selection: { anchor: ann.state.doc.length } });
  fireEvent.click(screen.getByRole("button", { name: "Bulleted list" }));
  await settle();
  expect(daemon.liveText("plan-1", "goals")).toBe("**Ship** the editor.\n- Test it.");
  expect(viewIn("bo").state.doc.toString()).toBe("**Ship** the editor.\n- Test it.");
});

it("keeps the local caret on the same text when a teammate inserts before it, after an emoji too", async () => {
  const { ann, bo } = await twoWriters("a😀bc");
  // Ann's caret sits between "b" and "c" (UTF-16 offset 4: the emoji is two units).
  ann.dispatch({ selection: { anchor: 4 } });
  // Bo writes before the emoji, and just after it.
  bo.dispatch({ changes: [{ from: 0, insert: "Hi " }, { from: 3, insert: "Q" }] });
  await waitFor(() => expect(ann.state.doc.toString()).toBe("Hi a😀Qbc"));
  const head = ann.state.selection.main.head;
  expect(ann.state.doc.sliceString(0, head)).toBe("Hi a😀Qb");
  expect(ann.state.doc.sliceString(head)).toBe("c");
});
