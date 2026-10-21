import { useState } from "react";
import { InboxList } from "../../components/InboxList";
import { InboxPanel, type InboxTarget } from "../../components/InboxPanel";
import { ErrorBlock, QueryView, Spinner } from "../../components/Status";
import { WorkspaceBanner } from "../../components/WorkspaceBanner";
import type { WorkspaceOverviewEntry } from "../../lib/api";
import { useApp } from "../../lib/app";
import { ago } from "../../lib/format";
import { THREAD_PHASE, WORKSPACE_STATUS, phaseTone } from "../../lib/labels";
import { byNewest, inboxRows, isLife } from "../../lib/overview";
import { usePrefs } from "../../lib/prefs";
import { Link } from "../../lib/router";
import { threadPath, useThreads, workThreadRows } from "../../lib/threads";

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
  const threads = useThreads();
  const rows = workThreadRows(threads.data, prefs.life);
  const unavailable = entries.filter((e) => !e.available);

  return (
    <div className="content">
      <div className="page-head">
        <div className="grow">
          <h1 className="page-title">工作</h1>
        </div>
        <Link className="btn primary" to="/work/new">
          新线程
        </Link>
      </div>
      {overview.error !== undefined && <ErrorBlock error={overview.error} onRetry={overview.reload} stale={data !== undefined} />}
      {data === undefined && overview.error === undefined && (
        <p className="empty">
          <Spinner /> 加载中…
        </p>
      )}
      {entries.length > 0 && <WorkspaceChips entries={entries} />}
      {data !== undefined && entries.length === 0 && (
        <div className="banner">
          <div className="banner-title">还没有登记工作区</div>
          <div className="meta">
            在终端里进入你的仓库运行 <span className="mono">ehai add</span>，登记并启动一个工作区，它的线程就会显示在这里。
          </div>
        </div>
      )}
      {unavailable.map((e) => (
        <WorkspaceBanner
          key={e.workspace.workspace_id}
          entry={e}
          title={
            <>
              <span className="mono">{e.workspace.workspace_id}</span> 没在运行
            </>
          }
        >
          <div className="meta">它的待处理事项和 Run 暂时看不到，启动后才会显示。</div>
        </WorkspaceBanner>
      ))}

      <section className="section" aria-labelledby="work-inbox">
        <div className="section-head">
          <h2 id="work-inbox" className="h2">
            待处理
          </h2>
          {inbox.length > 0 && <span className="nav-count">{inbox.length}</span>}
        </div>
        <InboxList rows={inbox} onOpen={setTarget} />
      </section>

      <section className="section" aria-labelledby="work-threads">
        <div className="section-head">
          <h2 id="work-threads" className="h2 grow">
            线程
          </h2>
        </div>
        <QueryView query={threads}>
          {() =>
            rows.length === 0 ? (
              <p className="empty">还没有线程。点「新线程」写下要做成的事，Planner 会先给出方案。</p>
            ) : (
              <div className="list">
                {rows.map((row) => (
                  <Link key={threadPath(row)} className="row" to={threadPath(row)}>
                    <span className="grow">
                      <span className="title">{row.thread.objective}</span>
                      <span className="meta">
                        {row.workspaceId} / {row.thread.project_name} · {ago(row.thread.last_activity_at)}
                      </span>
                    </span>
                    <span className={`tag ${phaseTone(row.thread.phase)}`}>{THREAD_PHASE[row.thread.phase]}</span>
                  </Link>
                ))}
              </div>
            )
          }
        </QueryView>
      </section>

      {target && <InboxPanel target={target} onClose={() => setTarget(null)} />}
    </div>
  );
}
