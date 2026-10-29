import { useCallback, useEffect, useRef, useState } from "react";

const REFRESH_MS = 10000;

export function useApi<T>(path: string) {
  const [state, setState] = useState<{ path: string; data: T | null; error: string | null }>({
    path,
    data: null,
    error: null,
  });
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    const controller = new AbortController();
    const load = async () => {
      try {
        const response = await fetch(path, { signal: controller.signal, cache: "no-store" });
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body.error || `The server answered ${response.status}`);
        }
        const data = (await response.json()) as T;
        if (alive.current) setState({ path, data, error: null });
      } catch (error) {
        if (controller.signal.aborted || !alive.current) return;
        const message = error instanceof Error ? error.message : "Request failed";
        setState((old) => ({ path, data: old.path === path ? old.data : null, error: message }));
      }
    };
    void load();
    const timer = window.setInterval(load, REFRESH_MS);
    return () => {
      alive.current = false;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [path, tick]);

  return {
    data: state.path === path ? state.data : null,
    error: state.path === path ? state.error : null,
    reload,
  };
}

export async function decide(
  incidentId: string,
  action: "approve" | "reject",
  reason: string,
): Promise<{ message: string; agent_running: boolean }> {
  const response = await fetch(`/api/incidents/${encodeURIComponent(incidentId)}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, reason: reason.trim() || null }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `The server answered ${response.status}`);
  return body;
}
