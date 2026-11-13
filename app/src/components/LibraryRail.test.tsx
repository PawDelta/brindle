import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { FolderGroup } from "../api/client";
import { setReaderDoc } from "../store/libraryReader";
import { LibraryRail } from "./LibraryRail";

afterEach(() => {
  cleanup();
  setReaderDoc(null);
});

const folders: FolderGroup[] = [
  { scope: { kind: "user", id: "u1" }, folders: [{ id: "fld_a", name: "Drafts", visibility: "private" }] },
  { scope: { kind: "group", id: "t1", name: "Platform team" }, folders: [{ id: "fld_b", name: "Cloud", visibility: "shared" }] },
];

it("lists folders by scope, creates one once even when submitted twice, and moves the design open in the reader", async () => {
  let finish!: () => void;
  const onCreateFolder = vi.fn().mockReturnValue(new Promise<void>((resolve) => (finish = resolve)));
  const onMoveDoc = vi.fn().mockRejectedValueOnce(new Error("refused")).mockResolvedValue(undefined);
  render(<LibraryRail route={{ screen: "knowledge" }} folders={folders} navigate={vi.fn()} onBack={vi.fn()} onCreateFolder={onCreateFolder} onMoveDoc={onMoveDoc} />);
  expect(screen.getByTestId("folders-team:t1").textContent).toContain("Cloud");
  expect(screen.queryByText("Move here")).toBeNull(); // no design open: nothing to move

  fireEvent.click(screen.getByText("+ New"));
  fireEvent.change(screen.getByLabelText("Folder name"), { target: { value: " Cloud 2 " } });
  fireEvent.change(screen.getByLabelText("Folder in"), { target: { value: "team:t1" } });
  fireEvent.change(screen.getByLabelText("Who can see it"), { target: { value: "shared" } });
  // Visible to the team, but only the creator may add: the two are separate choices.
  const form = screen.getByRole("form", { name: "New folder" });
  fireEvent.submit(form);
  fireEvent.submit(form);
  expect((screen.getByText("Create folder") as HTMLButtonElement).disabled).toBe(true);
  await act(async () => finish());
  expect(onCreateFolder.mock.calls).toEqual([["team:t1", "Cloud 2", "shared", "owner"]]);
  await waitFor(() => expect(screen.queryByRole("form", { name: "New folder" })).toBeNull());

  // The design open in the reader (opened, switched, or closed there) is the one moved.
  act(() => setReaderDoc("A"));
  act(() => setReaderDoc("B"));
  fireEvent.click(screen.getByLabelText("Move here: Cloud"));
  await screen.findByRole("alert"); // a refused move is shown
  fireEvent.click(screen.getByLabelText("Move here: Cloud"));
  await waitFor(() => expect(onMoveDoc).toHaveBeenLastCalledWith("B", "team:t1", "fld_b"));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  act(() => setReaderDoc(null));
  expect(screen.queryByText("Move here")).toBeNull();
});
