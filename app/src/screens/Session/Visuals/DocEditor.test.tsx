import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { EditorView } from "@codemirror/view";
import { afterEach, expect, it } from "vitest";
import { FakeDaemon } from "../../../test/fakeDaemon";
import { DocEditor } from "./DocEditor";

afterEach(cleanup);

/* Two people with the same section open, each through their own client of the daemon. */
async function twoWriters(text: string) {
  const daemon = new FakeDaemon();
  daemon.seedLive("plan-1", { goals: text });
  const { container } = render(
    <>
      <div data-testid="ann"><DocEditor client={daemon.client()} visual="plan-1" section="goals" name="Ann" label="Ann's text" /></div>
      <div data-testid="bo"><DocEditor client={daemon.client()} visual="plan-1" section="goals" name="Bo" label="Bo's text" /></div>
    </>,
  );
  const viewIn = (who: string) => {
    const dom = screen.getByTestId(who).querySelector<HTMLElement>(".cm-editor");
    return dom ? EditorView.findFromDOM(dom) : null;
  };
  await waitFor(() => expect(viewIn("ann") && viewIn("bo")).toBeTruthy());
  return { daemon, container, ann: viewIn("ann")!, bo: viewIn("bo")! };
}

it("brings two writers' concurrent edits to the same text, through the daemon", async () => {
  const { daemon, ann, bo } = await twoWriters("Ship the editor.");
  // Both type before either hears of the other's change.
  ann.dispatch({ changes: { from: 0, insert: "First, " } });
  bo.dispatch({ changes: { from: bo.state.doc.length, insert: " Then test it." } });
  await waitFor(() => expect(ann.state.doc.toString()).toBe("First, Ship the editor. Then test it."));
  expect(bo.state.doc.toString()).toBe(ann.state.doc.toString());
  expect(daemon.liveText("plan-1", "goals")).toBe(ann.state.doc.toString());
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
