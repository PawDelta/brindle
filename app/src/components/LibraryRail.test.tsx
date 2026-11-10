import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { FolderGroup } from "../api/client";
import { LibraryRail } from "./LibraryRail";

afterEach(cleanup);

const folders: FolderGroup[] = [
  { scope: { kind: "user", id: "u1" }, folders: [{ id: "fld_a", name: "Drafts", visibility: "private" }] },
  { scope: { kind: "group", id: "t1", name: "Platform team" }, folders: [{ id: "fld_b", name: "Cloud", visibility: "shared" }] },
];

it("lists folders by scope, creates one in a chosen scope, and moves the open design into one", async () => {
  const onCreateFolder = vi.fn().mockResolvedValue(undefined);
  const onMoveDoc = vi.fn().mockRejectedValueOnce(new Error("not allowed")).mockResolvedValue(undefined);
  const props = { folders, navigate: vi.fn(), onBack: vi.fn(), onCreateFolder, onMoveDoc };
  const { rerender } = render(<LibraryRail route={{ screen: "knowledge" }} {...props} />);
  expect(screen.getByTestId("folders-team:t1").textContent).toContain("Cloud");
  expect(screen.queryByText("Move here")).toBeNull(); // no design open: nothing to move

  fireEvent.click(screen.getByText("+ New"));
  fireEvent.change(screen.getByLabelText("Folder name"), { target: { value: " Cloud 2 " } });
  fireEvent.change(screen.getByLabelText("Folder in"), { target: { value: "team:t1" } });
  fireEvent.change(screen.getByLabelText("Who can see it"), { target: { value: "shared" } });
  fireEvent.click(screen.getByText("Create folder"));
  await waitFor(() => expect(onCreateFolder).toHaveBeenCalledWith("team:t1", "Cloud 2", "shared"));
  await waitFor(() => expect(screen.queryByRole("form", { name: "New folder" })).toBeNull());

  rerender(<LibraryRail route={{ screen: "knowledge", view: "docs", doc: "d1" }} {...props} />);
  fireEvent.click(screen.getByLabelText("Move here: Cloud"));
  expect((await screen.findByRole("alert")).textContent).toContain("not allowed"); // a refused move is shown
  fireEvent.click(screen.getByLabelText("Move here: Cloud"));
  await waitFor(() => expect(onMoveDoc).toHaveBeenLastCalledWith("d1", "team:t1", "fld_b"));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
});
