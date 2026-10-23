import { createContext, useContext, useSyncExternalStore } from "react";
import type { AppState, AppStore } from "./store";

export const StoreContext = createContext<AppStore | null>(null);

/* The store itself, for a component that subscribes to it on its own terms (the goal panel). */
export function useStore(): AppStore {
  const store = useContext(StoreContext);
  if (!store) throw new Error("useStore needs a <StoreContext.Provider>");
  return store;
}

/* A slice of the app state; re-renders when that slice changes. The selector
   must return something already in the state (or a primitive), not a new object. */
export function useAppState<T>(select: (state: AppState) => T): T {
  const store = useContext(StoreContext);
  if (!store) throw new Error("useAppState needs a <StoreContext.Provider>");
  return useSyncExternalStore(store.subscribe, () => select(store.getState()));
}
