import { useState, type ReactNode } from "react";
import { ErrorBlock, Spinner } from "../../components/Status";
import { canStart, WorkspaceBanner } from "../../components/WorkspaceBanner";
import { core } from "../../lib/api";
import { useApp } from "../../lib/app";
import { entries, projectRows } from "../../lib/overview";
import { setPrefs, usePrefs } from "../../lib/prefs";
import { useSubmission } from "../../lib/submit";

export type Life = { readonly workspaceId: string; readonly projectId: string; readonly projectName: string };

/**
 * The life zone works on one Project. The core has no zone tag yet, so the choice is a
 * per-browser preference; until one is chosen, offer to pick or create the Project.
 */
export function LifeGate({ children }: { children: (life: Life) => ReactNode }) {
  const { overview } = useApp();
  const prefs = usePrefs();
  const binding = prefs.life;
  if (binding === null) return <LifeSetup />;
  const entry = entries(overview.data).find((e) => e.workspace.workspace_id === binding.workspaceId);
  const project = entry?.projects?.find((p) => p.project.project_id === binding.projectId);
  if (overview.data !== undefined && entry !== undefined && entry.available && project === undefined) {
    return (
      <div className="content">
        <div className="banner attn">
          <div className="banner-title">找不到生活项目</div>
          <div className="meta">
            工作区 <span className="mono">{binding.workspaceId}</span> 里没有之前选定的项目，可能已经换了数据目录。
          </div>
          <div>
            <button type="button" className="btn small" onClick={() => setPrefs({ life: null })}>
              重新选择
            </button>
          </div>
        </div>
      </div>
    );
  }
  const banner = entry !== undefined && !entry.available && (
    <div className="content" style={{ paddingBottom: 0 }}>
      <WorkspaceBanner
        entry={entry}
        title={
          <>
            生活项目所在的工作区 <span className="mono">{binding.workspaceId}</span> 不可用
          </>
        }
      >
        {!canStart(entry) && <div className="meta">下面的内容可能无法读取或已经过期。</div>}
      </WorkspaceBanner>
    </div>
  );
  // A workspace that is not running cannot answer; show only how to start it.
  if (entry !== undefined && !entry.available && canStart(entry)) return banner;
  return (
    <>
      {banner}
      {children({
        workspaceId: binding.workspaceId,
        projectId: binding.projectId,
        projectName: project?.project.name ?? "生活",
      })}
    </>
  );
}

function LifeSetup() {
  const { overview, touch } = useApp();
  const rows = projectRows(overview.data);
  const available = entries(overview.data).filter((e) => e.available);
  const [workspaceId, setWorkspaceId] = useState("");
  const [name, setName] = useState("生活");
  const submission = useSubmission("create-life-project");
  const chosenWorkspace = workspaceId || available[0]?.workspace.workspace_id || "";

  async function create() {
    const result = await submission.run((key) =>
      core(chosenWorkspace).createProject({ idempotency_key: key, name: name.trim() }),
    );
    if (result) {
      touch(chosenWorkspace);
      setPrefs({ life: { workspaceId: chosenWorkspace, projectId: result.data.project_id } });
    }
  }

  return (
    <div className="content">
      <h1 className="page-title">生活</h1>
      <p className="body muted">
        生活分区使用一个 Project 存放待办、定时回顾和录入记录。选择已有的项目，或者新建一个。这个选择保存在当前浏览器里。
      </p>
      {overview.error !== undefined && <ErrorBlock error={overview.error} onRetry={overview.reload} />}
      {overview.data === undefined && overview.error === undefined && (
        <p className="empty">
          <Spinner /> 正在读取工作区…
        </p>
      )}
      {rows.length > 0 && (
        <section className="section">
          <h2 className="h2">用已有项目</h2>
          <div className="list">
            {rows.map(({ workspaceId: ws, detail }) => (
              <button
                key={ws + detail.project.project_id}
                type="button"
                className="row"
                onClick={() => setPrefs({ life: { workspaceId: ws, projectId: detail.project.project_id } })}
              >
                <span className="grow">
                  <span className="title">{detail.project.name}</span>
                  <span className="meta">
                    {ws} · {detail.project.goal_count} 个目标
                  </span>
                </span>
              </button>
            ))}
          </div>
        </section>
      )}
      {available.length > 0 && (
        <section className="section">
          <h2 className="h2">新建生活项目</h2>
          <div className="split">
            <label className="field">
              <span>工作区</span>
              <select className="select" value={chosenWorkspace} onChange={(e) => setWorkspaceId(e.target.value)}>
                {available.map((e) => (
                  <option key={e.workspace.workspace_id} value={e.workspace.workspace_id}>
                    {e.workspace.workspace_id}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>名称</span>
              <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
            </label>
          </div>
          {submission.error !== undefined && <ErrorBlock error={submission.error} />}
          <div>
            <button
              type="button"
              className="btn primary"
              disabled={submission.pending || !name.trim() || !chosenWorkspace}
              onClick={() => void create()}
            >
              新建并使用
            </button>
          </div>
        </section>
      )}
      {overview.data !== undefined && available.length === 0 && (
        <div className="banner">
          <div className="meta">没有可用的工作区。先在管理层登记并启动一个工作区。</div>
        </div>
      )}
    </div>
  );
}
