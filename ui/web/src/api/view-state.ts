import { useState, type Dispatch, type SetStateAction } from "react";
// Volatile UI state survives navigation. Drafts and operational records live in the service.
const views = new Map<string, unknown>();
export const navigation = new Map<string, string>();
export const questionDrafts = new Map<string, string>();
export function useViewState<T>(
  key: string,
  initial: T,
): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() =>
    views.has(key) ? (views.get(key) as T) : initial,
  );
  const save: Dispatch<SetStateAction<T>> = (next) =>
    setValue((previous) => {
      const v =
        typeof next === "function" ? (next as (v: T) => T)(previous) : next;
      views.set(key, v);
      return v;
    });
  return [value, save];
}

export function saveViewState(key: string, value: unknown) {
  views.set(key, value);
}

export function forgetAttack(eid: string, sessions: string[]) {
  navigation.delete(eid);
  for (const key of views.keys())
    if (key.startsWith(eid + ".")) views.delete(key);
  for (const key of questionDrafts.keys()) {
    if (sessions.some((sid) => key.startsWith(sid))) questionDrafts.delete(key);
  }
  localStorage.removeItem("zero.scroll." + eid);
}
