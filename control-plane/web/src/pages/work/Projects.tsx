import { useState } from "react";
import { InboxList } from "../../components/InboxList";
import { InboxPanel, type InboxTarget } from "../../components/InboxPanel";
import { ErrorBlock, QueryView, RunStatusIcon, Spinner } from "../../components/Status";
import { core } from "../../lib/api";
import { useApp, useQuery } from "../../lib/app";
import { ago } from "../../lib/format";
import { RUN_STATUS } from "../../lib/labels";
import { byNewest, isLife, nodeProgress, projectRows } from "../../lib/overview";
import { usePrefs } from "../../lib/prefs";
import { Link } from "../../lib/router";

export function ProjectsPage() {
  const { overview } = useApp();
  const prefs = usePrefs();
  const rows = projectRows(overview.data);
  return (
    <div className="content">
      <h1 className="page-title">项目</h1>
      {overview.error !== undefined && <ErrorBlock error={overview.error} onRetry={overview.reload} stale />}
      {overview.data === undefined && overview.error === undefined && (
        <p className="empty">
          <Spinner /> 加载中…
        </p>
      )}
      {overview.data !== undefined && rows.length === 0 && <p className="empty">还没有项目。</p>}
      <div className="list">
        {rows.map(({ workspaceId, detail }) => {
          const p = detail.project;
          const life = isLife(prefs.life, workspaceId, p.project_id);
          return (
            <Link
              key={workspaceId + p.project_id}
              className="row"
              to={life ? "/life" : `/work/w/${workspaceId}/projects/${p.project_id}`}
            >
              <span className="grow">
                <span className="title">
                  {p.name} {life && <span className="tag">生活</span>}
                </span>
                <span className="meta">
                  {workspaceId} · {p.goal_count} 个目标 · {p.run_count} 个 Run
                </span>
              </span>
            </Link>
          );
        })}
      </div>
    </div>
  );
}

export function ProjectPage({ workspaceId, projectId }: { workspaceId: string; projectId: string }) {
  const [target, setTarget] = useState<InboxTarget | null>(null);
  const project = useQuery(
    `project:${workspaceId}:${projectId}`,
    () => core(workspaceId).getProject(projectId).then((r) => r.data),
    { workspaces: [workspaceId] },
  );
  const inbox = useQuery(
    `project-inbox:${workspaceId}:${projectId}`,
    () => core(workspaceId).listInbox({ projectId }).then((r) => r.data),
    { workspaces: [workspaceId] },
  );
  return (
    <div className="content">
      <div className="page-head">
        <div className="grow">
          <div className="meta mono">{workspaceId}</div>
          <h1 className="page-title">{project.data?.project.name ?? "项目"}</h1>
        </div>
      </div>
      <section className="section">
        <h2 className="h2">待处理</h2>
        <QueryView query={inbox}>
          {(data) => (
            <InboxList
              rows={data.items.filter((i) => i.pending).map((item) => ({ workspaceId, item }))}
              onOpen={setTarget}
              showWorkspace={false}
            />
          )}
        </QueryView>
      </section>
      <QueryView query={project}>
        {(data) =>
          data.goals.length === 0 ? (
            <p className="empty">还没有目标。目前要用命令行或 MCP 创建目标、讨论方案。</p>
          ) : (
            <section className="section">
              <h2 className="h2">目标</h2>
              {byNewest([...data.goals], (g) => g.created_at).map((goal) => (
                <div key={goal.goal_id} className="card">
                  <div>
                    <div className="title" style={{ fontWeight: 500 }}>
                      {goal.objective}
                    </div>
                    <div className="meta">
                      {goal.status === "satisfied" ? "已满足" : "进行中"} · {ago(goal.created_at)} · 方案{" "}
                      {goal.plans.map((p) => `v${p.version}${p.status === "approved" ? "（已批准）" : ""}`).join("、") || "无"}
                    </div>
                  </div>
                  {goal.runs.length > 0 && (
                    <div className="list">
                      {byNewest([...goal.runs], (r) => r.run.created_at).map((summary) => (
                        <Link
                          key={summary.run.run_id}
                          className="row"
                          to={`/work/w/${workspaceId}/runs/${summary.run.run_id}`}
                        >
                          <RunStatusIcon status={summary.run.status} />
                          <span className="grow">
                            <span className="title">
                              {RUN_STATUS[summary.run.status]}
                              {summary.run.status_reason ? ` · ${summary.run.status_reason}` : ""}
                            </span>
                            <span className="meta">
                              {nodeProgress(summary.node_counts)} · {summary.completed_results.length} 个通过检查的成果 ·{" "}
                              {ago(summary.run.created_at)}
                            </span>
                          </span>
                        </Link>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </section>
          )
        }
      </QueryView>
      {target && <InboxPanel target={target} onClose={() => setTarget(null)} />}
    </div>
  );
}
