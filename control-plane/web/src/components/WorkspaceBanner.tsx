import type { ReactNode } from "react";
import { manager, type WorkspaceOverviewEntry } from "../lib/api";
import { useApp } from "../lib/app";
import { WORKSPACE_STATUS } from "../lib/labels";
import { useSubmission } from "../lib/submit";
import { ErrorBlock, Spinner } from "./Status";
import { useToast } from "./Toast";

/** A stopped workspace can be started from here; any other failure is only reported. */
export function canStart(entry: WorkspaceOverviewEntry): boolean {
  const status = entry.workspace.status;
  return status === "registered" || status === "stopped" || status === "failed";
}

export function WorkspaceBanner({ entry, title, children }: { entry: WorkspaceOverviewEntry; title: ReactNode; children?: ReactNode }) {
  const { overview } = useApp();
  const toast = useToast();
  const submission = useSubmission("workspace-start");
  const id = entry.workspace.workspace_id;

  async function start() {
    const result = await submission.run(() => manager.startWorkspace(id));
    overview.reload();
    if (result) toast(`已启动 ${id}`);
  }

  return (
    <div className="banner bad" role="alert">
      <div className="banner-title">{title}</div>
      <div className="meta">
        {WORKSPACE_STATUS[entry.workspace.status]}
        {entry.error && !canStart(entry) ? ` · ${entry.error.message}` : ""}
      </div>
      {children}
      {submission.error !== undefined && <ErrorBlock error={submission.error} />}
      {canStart(entry) && (
        <div>
          <button type="button" className="btn small" disabled={submission.pending} onClick={() => void start()}>
            {submission.pending ? <Spinner /> : `启动 ${id}`}
          </button>
        </div>
      )}
    </div>
  );
}
