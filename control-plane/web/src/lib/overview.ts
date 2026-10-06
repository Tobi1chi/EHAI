import type {
  InboxItem,
  ProjectDetailView,
  ProjectGoalSummary,
  ProjectRunSummary,
  WorkspaceOverviewData,
  WorkspaceOverviewEntry,
} from "./api";
import type { LifeBinding } from "./prefs";

// Read-only projections over the manager overview. Each workspace is a separate core, so
// rows always carry their workspace; nothing here merges workspaces into one snapshot.

export type InboxRow = { readonly workspaceId: string; readonly item: InboxItem };
export type ProjectRow = { readonly workspaceId: string; readonly detail: ProjectDetailView };
export type RunRow = {
  readonly workspaceId: string;
  readonly project: ProjectDetailView["project"];
  readonly goal: ProjectGoalSummary;
  readonly summary: ProjectRunSummary;
};

export function isLife(life: LifeBinding | null, workspaceId: string, projectId: string): boolean {
  return life !== null && life.workspaceId === workspaceId && life.projectId === projectId;
}

export function entries(data: WorkspaceOverviewData | undefined): ReadonlyArray<WorkspaceOverviewEntry> {
  return data?.workspaces ?? [];
}

export function inboxRows(data: WorkspaceOverviewData | undefined): InboxRow[] {
  return entries(data).flatMap((entry) =>
    (entry.inbox?.items ?? [])
      .filter((item) => item.pending)
      .map((item) => ({ workspaceId: entry.workspace.workspace_id, item })),
  );
}

export function projectRows(data: WorkspaceOverviewData | undefined): ProjectRow[] {
  return entries(data).flatMap((entry) =>
    (entry.projects ?? []).map((detail) => ({ workspaceId: entry.workspace.workspace_id, detail })),
  );
}

export function runRows(data: WorkspaceOverviewData | undefined): RunRow[] {
  return projectRows(data).flatMap(({ workspaceId, detail }) =>
    detail.goals.flatMap((goal) =>
      goal.runs.map((summary) => ({ workspaceId, project: detail.project, goal, summary })),
    ),
  );
}

export function findRun(
  data: WorkspaceOverviewData | undefined,
  workspaceId: string,
  runId: string,
): RunRow | undefined {
  return runRows(data).find((r) => r.workspaceId === workspaceId && r.summary.run.run_id === runId);
}

export function zoneCounts(data: WorkspaceOverviewData | undefined, life: LifeBinding | null) {
  let lifeCount = 0;
  let workCount = 0;
  for (const { workspaceId, item } of inboxRows(data)) {
    if (isLife(life, workspaceId, item.owner.project_id)) lifeCount += 1;
    else workCount += 1;
  }
  return { life: lifeCount, work: workCount };
}

export function nodeProgress(counts: Readonly<Record<string, number>>): string {
  const total = Object.values(counts).reduce((sum, n) => sum + n, 0);
  const done = (counts["completed"] ?? 0) + (counts["pruned"] ?? 0);
  return total ? `${done}/${total} 节点` : "";
}

export function byNewest<T>(rows: T[], at: (row: T) => string | null | undefined): T[] {
  // Timestamps may carry different offsets, so compare instants rather than strings.
  const time = (row: T) => {
    const value = at(row);
    return value ? Date.parse(value) : -Infinity;
  };
  return [...rows].sort((a, b) => time(b) - time(a));
}
