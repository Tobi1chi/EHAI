import { useState } from "react";
import { manager, type AddWorkspaceFolderRequest, type WorkspaceDescriptor } from "../lib/api";
import { useApp, useQuery } from "../lib/app";
import { canPickFolder, pickFolder } from "../lib/desktop";
import { useSubmission } from "../lib/submit";
import { Panel } from "./Panel";
import { ErrorBlock, QueryView, Spinner } from "./Status";

type GitPermission = NonNullable<AddWorkspaceFolderRequest["git_permissions"]>[number];

const GIT_PERMISSIONS: ReadonlyArray<{ value: GitPermission; label: string }> = [
  { value: "git.read", label: "读取（status、log、diff）" },
  { value: "git.local_write", label: "本地写入（在自己的 worktree 里提交）" },
];

/** One command per line, split on spaces; quoting is not interpreted. */
function commands(text: string): string[][] {
  return text
    .split("\n")
    .map((line) => line.trim().split(/\s+/).filter(Boolean))
    .filter((argv) => argv.length > 0);
}

/**
 * Register a repository folder as a workspace with the engine's configured model and Pi, the
 * same as `ehai add`, then start it. Only a manager started by `ehai up` knows that configuration.
 */
export function AddFolderPanel({
  onClose,
  onAdded,
}: {
  onClose: () => void;
  onAdded: (workspace: WorkspaceDescriptor) => void;
}) {
  const { overview } = useApp();
  const engine = useQuery("local-engine", () => manager.getLocalEngine().then((r) => r.data));
  const submission = useSubmission("add-folder");
  const [path, setPath] = useState("");
  const [allowed, setAllowed] = useState("");
  const [git, setGit] = useState<ReadonlyArray<GitPermission>>(["git.read"]);

  async function choose() {
    const chosen = await pickFolder("选择项目文件夹");
    if (chosen !== null) setPath(chosen);
  }

  async function add() {
    const result = await submission.run(() =>
      manager.addWorkspaceFolder({
        path: path.trim(),
        allowed_commands: commands(allowed),
        git_permissions: git,
      }),
    );
    if (result === undefined) return;
    overview.reload();
    onAdded(result.data);
  }

  return (
    <Panel
      title="添加项目文件夹"
      subtitle="工作区"
      onClose={onClose}
      footer={
        <div className="actions">
          <button type="button" className="btn" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={submission.pending || !path.trim() || engine.data?.available !== true}
            onClick={() => void add()}
          >
            {submission.pending ? <Spinner /> : "添加并启动"}
          </button>
        </div>
      }
    >
      <QueryView query={engine}>
        {(local) =>
          local.available ? (
            <>
              <p className="meta">
                选一个 Git 仓库的根目录。EHAI 会用引擎配置的模型（{local.provider} · {local.model}）为它启动工作区；
                Worker 只在自己的 worktree 里改代码，不动你的工作目录。
              </p>
              <label className="field">
                <span>文件夹</span>
                <div className="actions" style={{ flexWrap: "nowrap" }}>
                  <input
                    className="input mono"
                    placeholder="/Users/you/code/project"
                    value={path}
                    onChange={(e) => setPath(e.target.value)}
                  />
                  {canPickFolder() && (
                    <button type="button" className="btn" onClick={() => void choose()}>
                      选择…
                    </button>
                  )}
                </div>
              </label>
              <label className="field">
                <span>允许 Worker 运行的命令</span>
                <textarea
                  className="textarea mono"
                  rows={3}
                  placeholder={"uv run pytest\nnpm test"}
                  value={allowed}
                  onChange={(e) => setAllowed(e.target.value)}
                />
                <span className="meta">每行一条，按空格分参数。不填则 Worker 只能读写文件、不能运行命令。</span>
              </label>
              <div className="field">
                <span>Git 权限</span>
                {GIT_PERMISSIONS.map((option) => (
                  <label key={option.value} className="inline-check">
                    <input
                      className="check"
                      type="checkbox"
                      checked={git.includes(option.value)}
                      onChange={(e) =>
                        setGit(e.target.checked ? [...git, option.value] : git.filter((v) => v !== option.value))
                      }
                    />
                    {option.label}
                  </label>
                ))}
              </div>
            </>
          ) : (
            <div className="banner">
              <div className="banner-title">这个管理器不是用 ehai up 启动的</div>
              <div className="meta">它不知道模型和 Pi 的配置，不能从文件夹添加。在终端运行：</div>
              <div className="code">ehai init{"\n"}ehai up{"\n"}ehai add &lt;文件夹&gt;</div>
            </div>
          )
        }
      </QueryView>
      {submission.error !== undefined && <ErrorBlock error={submission.error} />}
    </Panel>
  );
}
