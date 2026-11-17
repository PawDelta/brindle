import { render } from "@testing-library/react";
import { onTestFinished } from "vitest";
import type { ReactElement, ReactNode } from "react";
import { DaemonContext } from "../api/context";
import { PtyViewsProvider, type PtyViews } from "../api/ptyView";
import { ProvidersProvider } from "../store/providers";
import { SessionsProvider } from "../store/sessions";
import { createAppStore } from "../store/store";
import { startSync as startRealSync } from "../store/sync";
import { StoreContext } from "../store/useStore";
import type { FakeDaemon } from "./fakeDaemon";
import { standInViews } from "./panes";

type SyncOptions = Parameters<typeof startRealSync>[0];
type Harness = Pick<SyncOptions, "client" | "store">;

export function trackedSync(options: SyncOptions) {
  const sync = startRealSync(options);
  onTestFinished(() => sync.stop());
  return sync;
}

export function connectDaemon(daemon: FakeDaemon, client = daemon.client()) {
  const store = createAppStore();
  const sync = trackedSync({ client, store, connect: daemon.connect });
  return { client, store, sync };
}

export const renderApp = (app: ReactElement, views: PtyViews = standInViews) => render(<PtyViewsProvider value={views}>{app}</PtyViewsProvider>);

// Each screen opts into exactly the contexts its existing harness supplied.
export const inTestStore = (children: ReactNode, store: Harness["store"]) => <StoreContext.Provider value={store}>{children}</StoreContext.Provider>;

export function inTestContext(children: ReactNode, { client, store }: Harness, options: { daemon?: boolean; sessions?: boolean; providers?: boolean; views?: PtyViews | null } = {}) {
  if ("views" in options) children = <PtyViewsProvider value={options.views ?? null}>{children}</PtyViewsProvider>;
  if (options.providers) children = <ProvidersProvider client={client}>{children}</ProvidersProvider>;
  if (options.sessions) children = <SessionsProvider client={client}>{children}</SessionsProvider>;
  children = inTestStore(children, store);
  if (options.daemon) children = <DaemonContext.Provider value={client}>{children}</DaemonContext.Provider>;
  return children;
}
