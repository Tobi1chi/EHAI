import { CaptureDecision } from "../../components/InboxPanel";
import { Icon } from "../../components/Icon";
import { QueryView } from "../../components/Status";
import { core, type WorkflowRun } from "../../lib/api";
import { useQuery } from "../../lib/app";
import { ago, dateTime, shortId, when } from "../../lib/format";
import { byNewest } from "../../lib/overview";
import { Link } from "../../lib/router";
import { LifeGate, type Life } from "./LifeGate";
import { useWorkflowRuns } from "./data";

const STATUS: Record<WorkflowRun["status"], string> = {
  awaiting_confirmation: "等待确认",
  completed: "已完成",
  rejected: "没有保存",
};

function title(run: WorkflowRun): string {
  if (run.workflow === "life.review") {
    return `回顾 · ${dateTime(run.review?.as_of ?? run.created_at)}`;
  }
  return `录入 · ${run.proposed_tasks.length} 项`;
}

function trigger(run: WorkflowRun): string {
  if (run.routine_snapshot) return `定时「${run.routine_snapshot.name}」`;
  return "手动录入";
}

function Records({ life }: { life: Life }) {
  const runs = useWorkflowRuns(life);
  return (
    <div className="content">
      <div className="page-head">
        <div className="grow">
          <div className="meta">{life.projectName}</div>
          <h1 className="page-title">记录</h1>
        </div>
      </div>
      <QueryView query={runs}>
        {(data) =>
          data.length === 0 ? (
            <p className="empty">还没有记录。录入待办或定时回顾之后会显示在这里。</p>
          ) : (
            <div className="list">
              {byNewest([...data], (r) => r.created_at).map((run) => (
                <Link key={run.workflow_run_id} className="row" to={`/life/records/${run.workflow_run_id}`}>
                  <span className={run.status === "awaiting_confirmation" ? "attn" : "meta"}>
                    <Icon name={run.workflow === "life.review" ? "record" : "tasks"} />
                  </span>
                  <span className="grow">
                    <span className="title">{title(run)}</span>
                    <span className="meta">
                      {STATUS[run.status]} · {trigger(run)} · {ago(run.created_at)}
                      {run.review ? ` · ${run.review.open_tasks.length} 项没做完` : ""}
                    </span>
                  </span>
                </Link>
              ))}
            </div>
          )
        }
      </QueryView>
    </div>
  );
}

function Record({ life, workflowRunId }: { life: Life; workflowRunId: string }) {
  const client = core(life.workspaceId);
  const opts = { workspaces: [life.workspaceId] };
  const run = useQuery(
    `workflow-run:${life.workspaceId}:${workflowRunId}`,
    () => client.getWorkflowRun(workflowRunId).then((r) => r.data),
    opts,
  );
  const execution = useQuery(
    `workflow-execution:${life.workspaceId}:${workflowRunId}`,
    () => client.getWorkflowExecution(workflowRunId).then((r) => r.data),
    opts,
  );
  return (
    <div className="content">
      <div className="meta">
        <Link to="/life/records">记录</Link> / <span className="mono">{shortId(workflowRunId)}</span>
      </div>
      <QueryView query={run}>
        {(data) => (
          <>
            <div className="page-head">
              <div className="grow">
                <h1 className="page-title">{title(data)}</h1>
                <div className="meta">
                  {STATUS[data.status]} · {trigger(data)} · {dateTime(data.created_at)}
                </div>
              </div>
            </div>
            {data.coalesced_occurrences > 0 && (
              <div className="banner attn">
                <div className="meta">关机期间错过了 {data.coalesced_occurrences} 次，这次一起补上了。</div>
              </div>
            )}
            {data.workflow === "life.capture" && (
              <CaptureDecision workspaceId={life.workspaceId} run={data} onDone={run.reload} />
            )}
            {data.review && (
              <>
                <div className="stats">
                  <div className="stat">
                    <strong>{data.review.open_tasks.length}</strong>
                    <span className="meta">没做完</span>
                  </div>
                  <div className="stat">
                    <strong className={data.review.overdue_task_ids.length ? "bad" : undefined}>
                      {data.review.overdue_task_ids.length}
                    </strong>
                    <span className="meta">已过期</span>
                  </div>
                  <div className="stat">
                    <strong>{data.review.done_count}</strong>
                    <span className="meta">已完成</span>
                  </div>
                  <div className="stat">
                    <strong>{data.review.cancelled_count}</strong>
                    <span className="meta">已取消</span>
                  </div>
                </div>
                <section className="section">
                  <div className="section-head">
                    <h2 className="h2">当时没做完的</h2>
                    <span className="meta">之后的改动不会反映在这里</span>
                  </div>
                  <div className="list">
                    {data.review.open_tasks.map((task) => {
                      const overdue = data.review?.overdue_task_ids.includes(task.task_id);
                      return (
                        <div key={task.task_id} className="row">
                          <span className="grow">{task.title}</span>
                          {task.due_at && (
                            <span className={`meta${overdue ? " bad" : ""}`}>{when(task.due_at, new Date(data.review?.as_of ?? Date.now()))}</span>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </section>
              </>
            )}
          </>
        )}
      </QueryView>
      <details className="disclosure">
        <summary>技术细节</summary>
        <QueryView query={execution}>
          {(data) => (
            <>
              <div className="list">
                {data.steps.map((step) => (
                  <div key={step.step_execution_id} className="row" style={{ minHeight: 44 }}>
                    <span className="grow">
                      <span className="mono">
                        {step.contract.name} v{step.contract.version}
                      </span>
                    </span>
                    <span className="tag">{step.status === "completed" ? "完成" : step.status === "waiting" ? "等待" : "跳过"}</span>
                    <span className="meta">{dateTime(step.recorded_at)}</span>
                  </div>
                ))}
              </div>
              <p className="meta mono">
                {workflowRunId} · {data.run.definition.name} v{data.run.definition.version}
              </p>
            </>
          )}
        </QueryView>
      </details>
    </div>
  );
}

export function LifeRecords() {
  return <LifeGate>{(life) => <Records life={life} />}</LifeGate>;
}

export function LifeRecord({ workflowRunId }: { workflowRunId: string }) {
  return <LifeGate>{(life) => <Record life={life} workflowRunId={workflowRunId} />}</LifeGate>;
}
