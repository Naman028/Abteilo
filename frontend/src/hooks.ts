import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { ApiError, api } from "./api";
import type { Me } from "./types";

export const MeContext = createContext<{ me: Me; refresh: () => void } | null>(null);

export function useMe() {
  const ctx = useContext(MeContext);
  if (!ctx) throw new Error("useMe outside MeContext");
  return ctx;
}

export const hasCompany = (me: Me, perm: string) => me.company_permissions.includes(perm);

/** Loads a GET endpoint; `reload` re-fetches after a mutation. */
export function useLoad<T>(path: string | null) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!path) return;
    let alive = true;
    setError(null);
    api.get<T>(path).then(
      (d) => alive && setData(d),
      (e: ApiError) => alive && setError(e.message),
    );
    return () => {
      alive = false;
    };
  }, [path, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, reload };
}

/** Wraps a mutation: tracks busy state and shows the API's safe error message. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong.");
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []);
  return { busy, error, run, setError };
}
