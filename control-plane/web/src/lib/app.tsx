import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { manager, type WorkspaceOverviewData } from "./api";
import { followWorkspace, type LiveStatus } from "./live";

export type QueryState<T> = {
  readonly data: T | undefined;
  readonly error: unknown;
  readonly loading: boolean;
  readonly reload: () => void;
};

type AppData = {
  readonly overview: QueryState<WorkspaceOverviewData>;
  /** Bumped whenever a workspace reports a durable event; queries re-read on change. */
  readonly versions: Readonly<Record<string, number>>;
  readonly live: Readonly<Record<string, LiveStatus>>;
  /** Re-read everything that depends on a workspace, e.g. right after this page wrote to it. */
  readonly touch: (workspaceId: string) => void;
};

const AppContext = createContext<AppData | null>(null);

export function useApp(): AppData {
  const value = useContext(AppContext);
  if (value === null) throw new Error("useApp outside AppProvider");
  return value;
}

const OVERVIEW_INTERVAL_MS = 30000;

export function AppProvider({ children }: { children: ReactNode }) {
  const [overviewData, setOverviewData] = useState<WorkspaceOverviewData | undefined>();
  const [overviewError, setOverviewError] = useState<unknown>(undefined);
  const [overviewLoading, setOverviewLoading] = useState(true);
  const [overviewNonce, setOverviewNonce] = useState(0);
  const [versions, setVersions] = useState<Record<string, number>>({});
  const [live, setLive] = useState<Record<string, LiveStatus>>({});
  const timers = useRef(new Map<string, number>());
  const overviewTimer = useRef<number | undefined>(undefined);

  const reloadOverview = useCallback(() => setOverviewNonce((n) => n + 1), []);

  const scheduleOverview = useCallback(() => {
    window.clearTimeout(overviewTimer.current);
    overviewTimer.current = window.setTimeout(reloadOverview, 400);
  }, [reloadOverview]);

  const touch = useCallback(
    (workspaceId: string) => {
      // Coalesce bursts (a replayed backlog, or one write emitting several events).
      window.clearTimeout(timers.current.get(workspaceId));
      timers.current.set(
        workspaceId,
        window.setTimeout(() => {
          setVersions((v) => ({ ...v, [workspaceId]: (v[workspaceId] ?? 0) + 1 }));
        }, 250),
      );
      scheduleOverview();
    },
    [scheduleOverview],
  );

  useEffect(() => {
    let cancelled = false;
    setOverviewLoading(true);
    manager.getWorkspaceOverview().then(
      ({ data }) => {
        if (cancelled) return;
        setOverviewData(data);
        setOverviewError(undefined);
        setOverviewLoading(false);
      },
      (error: unknown) => {
        if (cancelled) return;
        setOverviewError(error);
        setOverviewLoading(false);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [overviewNonce]);

  useEffect(() => {
    const timer = window.setInterval(reloadOverview, OVERVIEW_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [reloadOverview]);

  // A starting or stopping workspace emits no events the page can follow; poll until it settles.
  const transitioning = (overviewData?.workspaces ?? []).some(
    (entry) => entry.workspace.status === "starting" || entry.workspace.status === "stopping",
  );
  useEffect(() => {
    if (!transitioning) return;
    const timer = window.setTimeout(reloadOverview, 1500);
    return () => window.clearTimeout(timer);
  }, [transitioning, overviewData, reloadOverview]);

  const availableKey = (overviewData?.workspaces ?? [])
    .filter((entry) => entry.available)
    .map((entry) => entry.workspace.workspace_id)
    .sort()
    .join(",");

  useEffect(() => {
    const ids = availableKey ? availableKey.split(",") : [];
    const controllers = ids.map((workspaceId) => {
      const controller = new AbortController();
      void followWorkspace(
        workspaceId,
        controller.signal,
        () => touch(workspaceId),
        (status) => setLive((current) => ({ ...current, [workspaceId]: status })),
      );
      return controller;
    });
    return () => {
      for (const controller of controllers) controller.abort();
    };
  }, [availableKey, touch]);

  const value = useMemo<AppData>(
    () => ({
      overview: {
        data: overviewData,
        error: overviewError,
        loading: overviewLoading,
        reload: reloadOverview,
      },
      versions,
      live,
      touch,
    }),
    [overviewData, overviewError, overviewLoading, reloadOverview, versions, live, touch],
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

const cache = new Map<string, unknown>();

type Snapshot<T> = {
  readonly key: string | null;
  readonly data: T | undefined;
  readonly error: unknown;
  readonly loading: boolean;
};

/**
 * Read one core query. Keeps the last good value while re-reading or after an error so a
 * failed refresh is shown as stale rather than as an empty list. Re-reads when any of the
 * given workspaces reports an event.
 */
export function useQuery<T>(
  key: string | null,
  fetcher: () => Promise<T>,
  options: { readonly workspaces?: ReadonlyArray<string>; readonly intervalMs?: number } = {},
): QueryState<T> {
  const { versions } = useApp();
  const liveKey = (options.workspaces ?? []).map((w) => `${w}:${versions[w] ?? 0}`).join(",");
  const [nonce, setNonce] = useState(0);
  const [snapshot, setSnapshot] = useState<Snapshot<T>>(() => ({
    key,
    data: key === null ? undefined : (cache.get(key) as T | undefined),
    error: undefined,
    loading: key !== null,
  }));
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    if (key === null) {
      setSnapshot({ key, data: undefined, error: undefined, loading: false });
      return;
    }
    let cancelled = false;
    setSnapshot((s) => ({
      key,
      data: s.key === key ? s.data : (cache.get(key) as T | undefined),
      error: s.key === key ? s.error : undefined,
      loading: true,
    }));
    fetcherRef.current().then(
      (data) => {
        if (cancelled) return;
        cache.set(key, data);
        setSnapshot({ key, data, error: undefined, loading: false });
      },
      (error: unknown) => {
        if (cancelled) return;
        setSnapshot((s) => ({ key, data: s.key === key ? s.data : undefined, error, loading: false }));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [key, liveKey, nonce]);

  useEffect(() => {
    if (!options.intervalMs || key === null) return;
    const timer = window.setInterval(() => setNonce((n) => n + 1), options.intervalMs);
    return () => window.clearInterval(timer);
  }, [key, options.intervalMs]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const current = snapshot.key === key;
  return {
    data: current ? snapshot.data : key === null ? undefined : (cache.get(key) as T | undefined),
    error: current ? snapshot.error : undefined,
    loading: current ? snapshot.loading : key !== null,
    reload,
  };
}
