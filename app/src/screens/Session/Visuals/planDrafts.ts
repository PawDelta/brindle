import { createContext, useContext, useState, useSyncExternalStore } from "react";
import type { AppStore } from "../../../store/store";
import { StoreContext } from "../../../store/useStore";

// Reader navigation unmounts the form. Keep unsent text in this app window, scoped to the doc
// and reader identity, without writing it to disk or sending it to the Library.
export const PlanDraftScope = createContext<string | null>(null);
const standaloneDrafts = new Map<string, unknown>();
const appDrafts = new WeakMap<AppStore, Map<string, unknown>>();
const listeners = new Set<() => void>();
const scopedKey = (scope: string, field: string) => JSON.stringify([scope, field]);
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
};

type Scope = { app: AppStore | null; doc: string } | null;

export function usePlanDraftScope(): Scope {
  const app = useContext(StoreContext);
  const doc = useContext(PlanDraftScope);
  return doc === null ? null : { app, doc };
}

export function readPlanDraft<T>(scope: Scope, field: string): T | null {
  if (!scope) return null;
  const drafts = scope.app ? appDrafts.get(scope.app) : standaloneDrafts;
  return (drafts?.get(scopedKey(scope.doc, field)) as T | undefined) ?? null;
}

export function savePlanDraft(scope: Scope, field: string, value: unknown): void {
  if (!scope) return;
  let drafts = scope.app ? appDrafts.get(scope.app) : standaloneDrafts;
  if (!drafts && scope.app && value !== null) {
    drafts = new Map();
    appDrafts.set(scope.app, drafts);
  }
  if (!drafts) return;
  const key = scopedKey(scope.doc, field);
  if ((drafts.get(key) ?? null) === value) return;
  if (value === null) drafts.delete(key);
  else drafts.set(key, value);
  listeners.forEach((listener) => listener());
}

export function usePlanDraft<T>(field: string) {
  const scope = usePlanDraftScope();
  const [local, setLocal] = useState<T | null>(null);
  const saved = useSyncExternalStore(subscribe, () => readPlanDraft<T>(scope, field));
  const value = scope === null ? local : saved;
  const update = (next: T | null) => {
    if (scope === null) setLocal(next);
    else savePlanDraft(scope, field, next);
  };
  // A reply can arrive after Back and reopen. Clear only the version that was sent.
  const clear = () => {
    if (scope === null) setLocal((current) => current === value ? null : current);
    else if (readPlanDraft<T>(scope, field) === value) update(null);
  };
  return [value, update, clear] as const;
}
