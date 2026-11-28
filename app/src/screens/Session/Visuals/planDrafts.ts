import { createContext, useContext, useState, useSyncExternalStore } from "react";

// Reader navigation unmounts the form. Keep unsent text in this app window, scoped to the doc
// and reader identity, without writing it to disk or sending it to the Library.
export const PlanDraftScope = createContext<string | null>(null);
const drafts = new Map<string, unknown>();
const listeners = new Set<() => void>();
const scopedKey = (scope: string, field: string) => JSON.stringify([scope, field]);
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
};

export function readPlanDraft<T>(scope: string | null, field: string): T | null {
  return scope === null ? null : (drafts.get(scopedKey(scope, field)) as T | undefined) ?? null;
}

export function savePlanDraft(scope: string | null, field: string, value: unknown): void {
  if (scope === null) return;
  const key = scopedKey(scope, field);
  if (value === null) drafts.delete(key);
  else drafts.set(key, value);
  listeners.forEach((listener) => listener());
}

export function usePlanDraft<T>(field: string) {
  const scope = useContext(PlanDraftScope);
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
