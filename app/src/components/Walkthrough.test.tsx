import { connectDaemon, renderApp } from "../test/harness";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { STEPS, WALKTHROUGH_KEY, Walkthrough, openWalkthrough } from "./Walkthrough";
import App from "../App";
import { ONBOARDED_KEY } from "../api/providers";
import { FakeDaemon, flush } from "../test/fakeDaemon";
import { REPO, root } from "../test/fixtures";

vi.mock("./PtyTerminal", () => import("../test/panes"));

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it.each([true, false])("navigates every step forward and back, including newly mounted targets (session: %s)", async (hasSession) => {
  if (hasSession) localStorage.setItem(ONBOARDED_KEY, "1");
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    return DOMRect.fromRect(this.dataset.walkthrough === "project-picker" ? { x: 30, y: 110, width: 40, height: 10 } : { x: 20, y: 100, width: 80, height: 20 });
  });
  const daemon = new FakeDaemon({
    repos: [{ repo_root: REPO, name: "proj" }],
    workspaces: hasSession ? [root] : [],
    sessions: hasSession ? [{ id: "sup1", workspace_id: "proj", profile: "supervisor", status: "idle", mode: "interactive", created_at: 1 }] : [],
  });
  const { client, store } = connectDaemon(daemon);
  renderApp(<App store={store} client={client} initialRoute={{ screen: "inbox" }} />);
  if (hasSession) {
    await screen.findByRole("region", { name: "Inbox" });
    expect(document.querySelector('[data-walkthrough="project-picker"]')).toBeNull();
    await act(async () => {
      await flush();
      openWalkthrough();
    });
  } else {
    // First launch starts the tour itself when setup finishes, overriding the old page.
    fireEvent.click(await screen.findByRole("button", { name: "Skip for now" }));
    await screen.findByTestId("walkthrough");
  }
  const check = async (target: string) => {
    expect(screen.getByTestId("walkthrough").getAttribute("data-step")).toBe(target);
    if (target === "inbox") await screen.findByRole("region", { name: "Inbox" });
    else if (target === "profile") expect((await screen.findByRole("tab", { name: "Account" })).getAttribute("aria-selected")).toBe("true");
    else if (target === "session-view" && hasSession) expect((await screen.findByTestId("terminal")).textContent).toBe("sup1");
    else {
      await screen.findByRole("form", { name: "Start a session" });
      expect(document.querySelector('[data-walkthrough="project-picker"]')).toBeTruthy();
    }
    await waitFor(() => expect(screen.queryByTestId("walkthrough-highlight")).toBeTruthy());
    if (target === "project-picker") await waitFor(() => expect(screen.getByTestId("walkthrough-highlight").style.width).toBe("48px"));
  };
  const press = (name: string) => fireEvent.click(within(screen.getByRole("dialog", { name: "Walkthrough" })).getByRole("button", { name }));
  for (let i = 0; i < STEPS.length; i++) {
    if (i === 1) {
      fireEvent.click(screen.getByRole("button", { name: "Inbox" }));
      expect(document.querySelector('[data-walkthrough="project-picker"]')).toBeNull();
    }
    if (i > 0) press("Next");
    await check(STEPS[i].target);
    if (STEPS[i].target === "profile") {
      fireEvent.click(screen.getByRole("tab", { name: "General" }));
      const reads = daemon.requests.filter((r) => r.path === "/sessions").length;
      await act(async () => {
        daemon.publish("message.queued", { agent_id: "sup1" });
        await flush();
      });
      expect(daemon.requests.filter((r) => r.path === "/sessions").length).toBeGreaterThan(reads);
      expect(screen.getByRole("tab", { name: "General" }).getAttribute("aria-selected")).toBe("true");
    }
  }
  for (let i = STEPS.length - 2; i >= 0; i--) {
    press("Back");
    await check(STEPS[i].target);
  }
  // Reopening on the first step must navigate again even though its index hasn't changed.
  fireEvent.click(screen.getByRole("button", { name: "Inbox" }));
  await screen.findByRole("region", { name: "Inbox" });
  act(() => openWalkthrough());
  await check(STEPS[0].target);
  for (let i = 1; i < STEPS.length; i++) press("Next");
  await check(STEPS[STEPS.length - 1].target);
  press("Done");
  expect(screen.queryByTestId("walkthrough")).toBeNull();
  if (hasSession) expect(screen.getByTestId("terminal").textContent).toBe("sup1");
  else expect(screen.getByRole("form", { name: "Start a session" })).toBeTruthy();
});

it("opens once after setup, follows its control as it scrolls, advances step by step, and once dismissed stays closed until reopened", () => {
  // jsdom lays nothing out: the control and its scrolling column (y 80–600, under a banner) report where
  // they are (the window is 1024×768).
  let at = { top: 100, left: 20, width: 80, height: 20 };
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.dataset.testid === "scroller") return DOMRect.fromRect({ x: 0, y: 80, width: 1024, height: 520 });
    return DOMRect.fromRect({ x: at.left, y: at.top, width: at.width, height: at.height });
  });
  const step = () => screen.getByTestId("walkthrough").getAttribute("data-step");
  const ring = () => screen.queryByTestId("walkthrough-highlight") as HTMLElement | null;
  const { unmount } = render(
    <>
      <div data-testid="scroller" style={{ overflowY: "auto" }}>
        <button type="button" data-walkthrough={STEPS[0].target}>
          New session
        </button>
      </div>
      <Walkthrough autoStart />
    </>,
  );
  expect(step()).toBe(STEPS[0].target);
  expect([ring()?.style.top, ring()?.style.height]).toEqual(["96px", "28px"]);
  // The column scrolls it half under its top edge: the ring follows and hugs the part still showing.
  at = { ...at, top: 70 };
  fireEvent.scroll(screen.getByTestId("scroller"));
  expect([ring()?.style.top, ring()?.style.height]).toEqual(["76px", "18px"]);
  // Wholly behind the column's edge, though still in the window: no ring over the banner, the card centred.
  at = { ...at, top: 40 };
  fireEvent.scroll(screen.getByTestId("scroller"));
  expect(ring()).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  expect(step()).toBe(STEPS[1].target);
  fireEvent.click(screen.getByRole("button", { name: "Back" }));
  expect(step()).toBe(STEPS[0].target);
  fireEvent.click(screen.getByRole("button", { name: "Skip" }));
  expect(screen.queryByTestId("walkthrough")).toBeNull();
  expect(localStorage.getItem(WALKTHROUGH_KEY)).toBe("done");
  unmount();

  // Dismissed: a later first-run mount doesn't open it again, but Help / the profile menu does.
  let ready = false;
  let page = "closed";
  const navigate = (step: (typeof STEPS)[number]) => {
    if (step.target === "session-view" && !ready) return false;
    page = step.target;
  };
  const { rerender } = render(<Walkthrough autoStart onStep={navigate} />);
  expect(screen.queryByTestId("walkthrough")).toBeNull();
  act(() => openWalkthrough());
  for (const s of STEPS.slice(0, -1)) {
    expect(step()).toBe(s.target);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
  }
  // Deferred initial data can reveal the final page once; later updates leave user selections alone.
  expect(page).toBe("profile");
  ready = true;
  rerender(<Walkthrough onStep={(s) => navigate(s)} />);
  expect(page).toBe("session-view");
  page = "worker";
  rerender(<Walkthrough onStep={(s) => navigate(s)} />);
  expect(page).toBe("worker");
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
  expect(screen.queryByTestId("walkthrough")).toBeNull();
});
