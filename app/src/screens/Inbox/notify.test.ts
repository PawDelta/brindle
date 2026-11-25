import { beforeEach, expect, it, vi } from "vitest";
import type { InboxItemDTO } from "../../api/schema";
import { createAppStore } from "../../store/store";
import { watchInbox } from "./notify";

beforeEach(() => localStorage.clear());

it("announces each PR once per kind across push gaps and CI reruns", () => {
  const store = createAppStore();
  const notify = vi.fn();
  const opened: InboxItemDTO = { kind: "pr_opened", reason: "PR opened", agent_id: "sup1", workspace_id: "proj", url: "https://github.com/o/r/pull/408" };
  const snapshot = (...inbox: InboxItemDTO[]) => store.dispatch({ type: "snapshot", workspaces: [], inbox });
  const stop = watchInbox(store, { notify, count: () => {} });

  snapshot(opened);
  expect(notify).toHaveBeenCalledTimes(0);
  // A push temporarily removes even a PR present at initial sync.
  snapshot();
  snapshot({ ...opened });
  expect(notify).toHaveBeenCalledTimes(0);

  // The same session can open another PR without an intervening empty poll.
  const next = { ...opened, url: "https://github.com/o/r/pull/409" };
  snapshot(next);
  expect(notify).toHaveBeenCalledTimes(1);
  snapshot();
  snapshot({ ...next });
  expect(notify).toHaveBeenCalledTimes(1);

  const failed = { ...next, kind: "pr_failed", detail: "test failed" };
  snapshot(failed);
  expect(notify).toHaveBeenCalledTimes(2);
  snapshot(next);
  snapshot();
  snapshot({ ...failed });
  snapshot(next);
  expect(notify).toHaveBeenCalledTimes(2);
  // A different failure on the same PR is news.
  snapshot({ ...failed, detail: "lint failed" });
  expect(notify).toHaveBeenCalledTimes(3);
  stop();
});

it("remembers a PR failure across relaunches when a worker hides the card", () => {
  const failed = {
    kind: "pr_failed", reason: "PR needs attention", url: "https://github.com/o/r/pull/549",
    detail: "conflicts", pr_head_sha: "abc",
  };
  const notify = vi.fn();
  const launch = () => {
    const store = createAppStore();
    return {
      stop: watchInbox(store, { notify, count: () => {} }),
      snapshot: (...inbox: InboxItemDTO[]) => store.dispatch({ type: "snapshot", workspaces: [], inbox }),
    };
  };
  const first = launch();
  first.snapshot();
  first.snapshot(failed);
  expect(notify).toHaveBeenCalledTimes(1);
  first.stop();

  const relaunched = launch();
  relaunched.snapshot();
  relaunched.snapshot(failed);
  expect(notify).toHaveBeenCalledTimes(1);
  relaunched.snapshot({ ...failed, pr_head_sha: "def" });
  expect(notify).toHaveBeenCalledTimes(2);
  relaunched.stop();
});
