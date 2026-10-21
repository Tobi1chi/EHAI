import { manager, type ThreadSummary } from "./api";
import { useApp, useQuery, type QueryState } from "./app";
import type { LifeBinding } from "./prefs";
import { isLife } from "./overview";

export type ThreadRow = { readonly workspaceId: string; readonly thread: ThreadSummary };

export type ThreadGroup = {
  readonly workspaceId: string;
  readonly available: boolean;
  readonly error: string | null;
  readonly threads: ReadonlyArray<ThreadSummary>;
};

/** Every workspace's threads as the manager aggregates them, re-read on any workspace event. */
export function useThreads(): QueryState<ReadonlyArray<ThreadGroup>> {
  const { overview } = useApp();
  const workspaces = (overview.data?.workspaces ?? [])
    .filter((entry) => entry.available)
    .map((entry) => entry.workspace.workspace_id);
  return useQuery(
    "threads",
    () =>
      manager.listThreads().then(({ data }) =>
        data.workspaces.map((entry) => ({
          workspaceId: entry.workspace.workspace_id,
          available: entry.available,
          error: entry.error ? `${entry.error.code}: ${entry.error.message}` : null,
          threads: entry.threads?.threads ?? [],
        })),
      ),
    { workspaces },
  );
}

/** Work threads, newest activity first; the life project's Goals stay in the life zone. */
export function workThreadRows(groups: ReadonlyArray<ThreadGroup> | undefined, life: LifeBinding | null): ThreadRow[] {
  const rows = (groups ?? []).flatMap((group) =>
    group.threads
      .filter((thread) => !isLife(life, group.workspaceId, thread.project_id))
      .map((thread) => ({ workspaceId: group.workspaceId, thread })),
  );
  return rows.sort((a, b) => Date.parse(b.thread.last_activity_at) - Date.parse(a.thread.last_activity_at));
}

export function threadPath(row: ThreadRow): string {
  return `/work/w/${row.workspaceId}/threads/${row.thread.goal_id}`;
}
