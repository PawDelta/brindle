import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { About } from "./About";

afterEach(cleanup);

it("shows the version and a license file's text from the shell, and offers nothing to show for a file the build lacks", async () => {
  const read = vi.fn(() =>
    Promise.resolve({
      version: "0.1.0-rc.9",
      files: [
        { name: "LICENSE", text: "Brindle License 1.0\n\nRequired Notice: Copyright PawDelta" },
        { name: "THIRD_PARTY_NOTICES.txt", text: null },
      ],
    }),
  );
  render(<About read={read} />);
  const license = await screen.findByTestId("about-LICENSE");
  expect(screen.getByTestId("about").textContent).toContain("brindle 0.1.0-rc.9");
  fireEvent.click(within(license).getByRole("button", { name: "Show" }));
  expect(screen.getByTestId("about-LICENSE-text").textContent).toBe("Brindle License 1.0\n\nRequired Notice: Copyright PawDelta");
  expect(within(screen.getByTestId("about-THIRD_PARTY_NOTICES.txt")).queryByRole("button")).toBeNull();
});
