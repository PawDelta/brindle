import { expect, it, vi } from "vitest";
import type { InboxItemDTO } from "../../api/schema";
import { createAppStore } from "../../store/store";
import { watchInbox } from "./notify";

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
