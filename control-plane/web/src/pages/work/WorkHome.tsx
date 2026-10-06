import { useState } from "react";
import { InboxList } from "../../components/InboxList";
import { InboxPanel, type InboxTarget } from "../../components/InboxPanel";
import { ErrorBlock, RunStatusIcon, Spinner } from "../../components/Status";
import { useToast } from "../../components/Toast";
import { WorkspaceBanner } from "../../components/WorkspaceBanner";
import { core, type WorkspaceOverviewEntry } from "../../lib/api";
import { useApp } from "../../lib/app";
import { ago } from "../../lib/format";
import { RUN_STATUS, WORKSPACE_STATUS } from "../../lib/labels";
import { byNewest, inboxRows, isLife, nodeProgress, runRows, type RunRow } from "../../lib/overview";
import { usePrefs } from "../../lib/prefs";
import { Link } from "../../lib/router";
import { useSubmission } from "../../lib/submit";

function WorkspaceChips({ entries }: { entries: ReadonlyArray<WorkspaceOverviewEntry> }) {
  return (
    <div className="tags">
      {entries.map((entry) => {
        const id = entry.workspace.workspace_id;
        const tone = entry.available ? "ok" : entry.workspace.status === "registered" || entry.workspace.status === "stopped" ? "" : "bad";
        return (
          <span
            key={id}
            className={`tag ${tone}`}
            title={entry.error ? `${entry.error.code}: ${entry.error.message}` : WORKSPACE_STATUS[entry.workspace.status]}
          >
            <span className="mono">{id}</span>
            {entry.available ? "" : ` · ${entry.error ? "不可用" : WORKSPACE_STATUS[entry.workspace.status]}`}
          </span>
        );
      })}
    </div>
  );
}

function ResumeButton({ row }: { row: RunRow }) {
  const { touch } = useApp();
  const toast = useToast();
  const submission = useSubmission("resume");
  async function resume() {
    const result = await submission.run((key) =>
      core(row.workspaceId).resumeRun(row.summary.run.run_id, { idempotency_key: key }),
    );
    if (result) {
      toast("已恢复 Run");
      touch(row.workspaceId);
    } else {
      toast("恢复失败，打开 Run 查看原因");
    }
  }
  return (
    <button type="button" className="btn small" disabled={submission.pending} onClick={() => void resume()}>
      {submission.pending ? <Spinner /> : "恢复"}
    </button>
  );
}

export function WorkHome() {
  const { overview } = useApp();
  const prefs = usePrefs();
  const [target, setTarget] = useState<InboxTarget | null>(null);
  const data = overview.data;
  const entries = data?.workspaces ?? [];
  const inbox = byNewest(
    inboxRows(data).filter((row) => !isLife(prefs.life, row.workspaceId, row.item.owner.project_id)),
    (row) => row.item.created_at,
  );
  const runs = runRows(data).filter((row) => !isLife(prefs.life, row.workspaceId, row.project.project_id));
  const active = byNewest(
    runs.filter((row) => ["running", "paused", "pending"].includes(row.summary.run.status)),
    (row) => row.summary.run.started_at ?? row.summary.run.created_at,
  );
  const finished = byNewest(
    runs.filter((row) => row.summary.run.status === "completed"),
    (row) => row.summary.run.ended_at,
  ).slice(0, 8);
  const unavailable = entries.filter((e) => !e.available);

  return (
    <div className="content">
      <div className="page-head">
        <div className="grow">
          <h1 className="page-title">工作</h1>
        </div>
      </div>
      {overview.error !== undefined && <ErrorBlock error={overview.error} onRetry={overview.reload} stale={data !== undefined} />}
      {data === undefined && overview.error === undefined && (
        <p className="empty">
          <Spinner /> 正在读取各工作区…
        </p>
      )}
      {entries.length > 0 && <WorkspaceChips entries={entries} />}
      {data !== undefined && entries.length === 0 && (
        <div className="banner">
          <div className="banner-title">还没有登记工作区</div>
          <div className="meta">
            用 <span className="mono">ehai --api-url &lt;管理层&gt; register-workspace</span> 登记并启动一个工作区后，这里会显示它的项目和待办。
          </div>
        </div>
      )}
      {unavailable.map((e) => (
        <WorkspaceBanner
          key={e.workspace.workspace_id}
          entry={e}
          title={
            <>
              <span className="mono">{e.workspace.workspace_id}</span> 不可用
            </>
          }
        >
          <div className="meta">这个工作区的待办和 Run 没有显示，不代表它们不存在。</div>
        </WorkspaceBanner>
      ))}

      <section className="section" aria-labelledby="work-inbox">
        <div className="section-head">
          <h2 id="work-inbox" className="h2">
            需要我处理
          </h2>
          {inbox.length > 0 && <span className="nav-count">{inbox.length}</span>}
        </div>
        <InboxList rows={inbox} onOpen={setTarget} />
      </section>

      <section className="section" aria-labelledby="work-active">
        <h2 id="work-active" className="h2">
          正在推进
        </h2>
        {active.length === 0 ? (
          <p className="empty">没有运行中或暂停的 Run</p>
        ) : (
          <div className="list">
            {active.map((row) => {
              const run = row.summary.run;
              return (
                <div key={row.workspaceId + run.run_id} className="row">
                  <RunStatusIcon status={run.status} />
                  <Link className="grow" to={`/work/w/${row.workspaceId}/runs/${run.run_id}`} style={{ textDecoration: "none" }}>
                    <span className="title">{row.goal.objective}</span>
                    <span className="meta">
                      {RUN_STATUS[run.status]}
                      {run.status_reason ? ` · ${run.status_reason}` : ""} · {row.workspaceId} / {row.project.name}
                      {nodeProgress(row.summary.node_counts) ? ` · ${nodeProgress(row.summary.node_counts)}` : ""}
                    </span>
                  </Link>
                  {run.status === "paused" && <ResumeButton row={row} />}
                </div>
              );
            })}
          </div>
        )}
      </section>

      <section className="section" aria-labelledby="work-done">
        <h2 id="work-done" className="h2">
          最近完成
        </h2>
        {finished.length === 0 ? (
          <p className="empty">还没有完成的 Run</p>
        ) : (
          <div className="list">
            {finished.map((row) => {
              const run = row.summary.run;
              const results = row.summary.completed_results.length;
              return (
                <Link key={row.workspaceId + run.run_id} className="row" to={`/work/w/${row.workspaceId}/runs/${run.run_id}`}>
                  <RunStatusIcon status={run.status} />
                  <span className="grow">
                    <span className="title">{row.goal.objective}</span>
                    <span className="meta">
                      {row.workspaceId} / {row.project.name} · {results} 个已验证成果 · {ago(run.ended_at)}
                    </span>
                  </span>
                </Link>
              );
            })}
          </div>
        )}
      </section>

      {target && <InboxPanel target={target} onClose={() => setTarget(null)} />}
    </div>
  );
}
