import { useState } from "react";
import { AddFolderPanel } from "../../components/AddFolderPanel";
import { ErrorBlock, Spinner } from "../../components/Status";
import { core } from "../../lib/api";
import { useApp } from "../../lib/app";
import { isLife } from "../../lib/overview";
import { usePrefs } from "../../lib/prefs";
import { navigate } from "../../lib/router";
import { useSubmission } from "../../lib/submit";
import { sendDiscussion } from "./discussion";

const NEW_PROJECT = "";
const ADD_FOLDER = "\u0000add-folder";

/**
 * Start a thread: a Goal in a chosen project, followed by the first planning message. The
 * Goal is created first so the thread exists even if planning is slow or fails; the planner
 * call then continues in the background while the thread page shows its progress.
 */
export function NewThread() {
  const { overview, touch } = useApp();
  const prefs = usePrefs();
  const workspaces = (overview.data?.workspaces ?? []).filter((e) => e.available);
  const [workspaceId, setWorkspaceId] = useState<string>(() => workspaces[0]?.workspace.workspace_id ?? "");
  const entry = workspaces.find((e) => e.workspace.workspace_id === workspaceId) ?? workspaces[0];
  const chosenWorkspace = entry?.workspace.workspace_id ?? "";
  const projects = (entry?.projects ?? []).filter(
    (p) => !isLife(prefs.life, chosenWorkspace, p.project.project_id),
  );
  const [projectChoice, setProjectChoice] = useState<string | null>(null);
  const projectId = projectChoice ?? projects[0]?.project.project_id ?? NEW_PROJECT;
  const [projectName, setProjectName] = useState("");
  const [adding, setAdding] = useState(false);
  const [objective, setObjective] = useState("");
  const [message, setMessage] = useState("");
  const createProject = useSubmission("project");
  const createGoal = useSubmission("goal");
  const pending = createProject.pending || createGoal.pending;

  async function submit() {
    if (!chosenWorkspace || !objective.trim()) return;
    const client = core(chosenWorkspace);
    let targetProject = projectId;
    if (targetProject === NEW_PROJECT) {
      const name = projectName.trim() || chosenWorkspace;
      const created = await createProject.run((key) => client.createProject({ idempotency_key: key, name }));
      if (created === undefined) return;
      targetProject = created.data.project_id;
      // A retry after a failed Goal must not create a second project.
      setProjectChoice(targetProject);
    }
    const goal = await createGoal.run((key) =>
      client.createGoal({ idempotency_key: key, project_id: targetProject, objective: objective.trim() }),
    );
    if (goal === undefined) return;
    const goalId = goal.data.goal_id;
    touch(chosenWorkspace);
    sendDiscussion(chosenWorkspace, goalId, null, message.trim() || objective.trim(), () => touch(chosenWorkspace));
    navigate(`/work/w/${chosenWorkspace}/threads/${goalId}`, { replace: true });
  }

  return (
    <div className="content">
      <div className="page-head">
        <div className="grow">
          <h1 className="page-title">新线程</h1>
          <p className="meta">写下要做成的事。Planner 会先调查仓库并给出方案，批准之前不会改动代码。</p>
        </div>
      </div>

      {overview.data !== undefined && workspaces.length === 0 && (
        <div className="banner">
          <div className="banner-title">没有在运行的工作区</div>
          <div className="meta">先添加一个项目文件夹，或启动已登记的工作区。</div>
          <div className="actions" style={{ justifyContent: "flex-start" }}>
            <button type="button" className="btn small primary" onClick={() => setAdding(true)}>
              添加项目文件夹…
            </button>
          </div>
        </div>
      )}

      <div className="split">
        <label className="field">
          <span>工作区</span>
          <select
            className="select"
            value={chosenWorkspace}
            onChange={(e) => {
              if (e.target.value === ADD_FOLDER) {
                setAdding(true);
                return;
              }
              setWorkspaceId(e.target.value);
              setProjectChoice(null);
            }}
          >
            {workspaces.map((e) => (
              <option key={e.workspace.workspace_id} value={e.workspace.workspace_id}>
                {e.workspace.workspace_id}
              </option>
            ))}
            <option value={ADD_FOLDER}>添加项目文件夹…</option>
          </select>
        </label>
        <label className="field">
          <span>项目</span>
          <select className="select" value={projectId} onChange={(e) => setProjectChoice(e.target.value)}>
            {projects.map((p) => (
              <option key={p.project.project_id} value={p.project.project_id}>
                {p.project.name}
              </option>
            ))}
            <option value={NEW_PROJECT}>新建项目…</option>
          </select>
        </label>
      </div>
      {projectId === NEW_PROJECT && (
        <label className="field">
          <span>项目名称</span>
          <input
            className="input"
            value={projectName}
            placeholder={chosenWorkspace || "项目名称"}
            onChange={(e) => setProjectName(e.target.value)}
          />
        </label>
      )}

      <label className="field">
        <span>目标</span>
        <input
          className="input"
          value={objective}
          placeholder="例如：修复日期格式化函数对空输入的处理，保持接口不变并通过测试"
          onChange={(e) => setObjective(e.target.value)}
        />
        <span className="meta">一个具体、能验收的结果。之后的讨论、方案和执行都归在这条线程里。</span>
      </label>

      <label className="field">
        <span>给 Planner 的说明（可选）</span>
        <textarea
          className="textarea"
          value={message}
          placeholder="范围、不能动的接口、用什么命令验收……不写就直接用目标开始规划。"
          onChange={(e) => setMessage(e.target.value)}
        />
      </label>

      {createProject.error !== undefined && <ErrorBlock error={createProject.error} />}
      {createGoal.error !== undefined && <ErrorBlock error={createGoal.error} />}
      <div className="actions">
        <button type="button" className="btn" onClick={() => navigate("/work")}>
          取消
        </button>
        <button
          type="button"
          className="btn primary large"
          disabled={pending || !chosenWorkspace || !objective.trim()}
          onClick={() => void submit()}
        >
          {pending ? <Spinner /> : "开始规划"}
        </button>
      </div>
      {adding && (
        <AddFolderPanel
          onClose={() => setAdding(false)}
          onAdded={(workspace) => {
            setAdding(false);
            setWorkspaceId(workspace.workspace_id);
            setProjectChoice(null);
          }}
        />
      )}
    </div>
  );
}
