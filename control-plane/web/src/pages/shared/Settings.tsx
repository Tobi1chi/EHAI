import { useEffect, useState } from "react";
import { AddFolderPanel } from "../../components/AddFolderPanel";
import { ErrorBlock, Spinner } from "../../components/Status";
import { useToast } from "../../components/Toast";
import { manager, type WorkspaceOverviewEntry } from "../../lib/api";
import { useApp } from "../../lib/app";
import { notificationPermission, notify, requestNotifications, type NotifyPermission } from "../../lib/desktop";
import { WORKSPACE_STATUS } from "../../lib/labels";
import { entries, projectRows } from "../../lib/overview";
import { setPrefs, usePrefs, type Theme } from "../../lib/prefs";
import { useSubmission } from "../../lib/submit";

function WorkspaceRow({ entry }: { entry: WorkspaceOverviewEntry }) {
  const { overview } = useApp();
  const toast = useToast();
  const submission = useSubmission("workspace");
  const w = entry.workspace;
  const running = w.status === "ready" || w.status === "starting";

  async function toggle() {
    const result = await submission.run(() =>
      running ? manager.stopWorkspace(w.workspace_id) : manager.startWorkspace(w.workspace_id),
    );
    overview.reload();
    if (result) toast(running ? `${w.workspace_id} 已停止` : `${w.workspace_id} 已启动`);
  }

  return (
    <div className="row" style={{ alignItems: "flex-start" }}>
      <span className="grow">
        <span className="title mono">{w.workspace_id}</span>
        <span className="meta">
          {WORKSPACE_STATUS[w.status]}
          {w.failure_category ? ` · ${w.failure_category}` : ""} · {w.runtime.worker_kind} Worker
          {w.runtime.worker_model ? ` · ${w.runtime.worker_model}` : ""}
        </span>
        <span className="meta mono" style={{ overflowWrap: "anywhere" }}>
          {w.path}
        </span>
        {entry.error && <span className="meta bad">{entry.error.message}</span>}
        {submission.error !== undefined && <ErrorBlock error={submission.error} />}
      </span>
      <button type="button" className="btn small" disabled={submission.pending} onClick={() => void toggle()}>
        {submission.pending ? <Spinner /> : running ? "停止" : "启动"}
      </button>
    </div>
  );
}

const PERMISSION: Record<NotifyPermission, string> = {
  granted: "系统已允许",
  denied: "系统拒绝了通知，请在系统或浏览器设置里打开",
  default: "还没有向系统申请",
  unsupported: "这个浏览器不支持系统通知",
};

function NotificationSetting() {
  const prefs = usePrefs();
  const [permission, setPermission] = useState<NotifyPermission | null>(null);

  useEffect(() => {
    void notificationPermission().then(setPermission);
  }, []);

  return (
    <div className="field">
      <span>系统通知</span>
      <label className="inline-check">
        <input
          className="check"
          type="checkbox"
          checked={prefs.notify}
          onChange={(e) => setPrefs({ notify: e.target.checked })}
        />
        窗口在后台时，线程需要你处理、方案等你审阅、完成或失败时通知我
      </label>
      <div className="actions" style={{ justifyContent: "flex-start", alignItems: "center" }}>
        {permission !== "granted" && permission !== "unsupported" && (
          <button
            type="button"
            className="btn small"
            onClick={() => void requestNotifications().then(setPermission)}
          >
            允许通知
          </button>
        )}
        {permission === "granted" && (
          <button type="button" className="btn small" onClick={() => notify("EHAI", "通知已经打开。")}>
            发一条测试通知
          </button>
        )}
        <span className="meta">{permission === null ? "" : PERMISSION[permission]}</span>
      </div>
    </div>
  );
}

const THEMES: ReadonlyArray<{ value: Theme; label: string }> = [
  { value: "system", label: "跟随系统" },
  { value: "light", label: "浅色" },
  { value: "dark", label: "深色" },
];

export function SettingsPage() {
  const prefs = usePrefs();
  const { overview } = useApp();
  const rows = projectRows(overview.data);
  const toast = useToast();
  const [adding, setAdding] = useState(false);
  const lifeValue = prefs.life ? `${prefs.life.workspaceId}|${prefs.life.projectId}` : "";

  return (
    <div className="content">
      <h1 className="page-title">设置</h1>
      <p className="meta">这些设置只保存在这个浏览器里。</p>

      <label className="field">
        <span>你的名字</span>
        <input
          className="input"
          placeholder="web"
          value={prefs.actor}
          onChange={(e) => setPrefs({ actor: e.target.value })}
        />
        <span className="meta">会记在你做的决定、回复和反馈旁边。不填就记为「web」。</span>
      </label>

      <label className="field">
        <span>「生活」用哪个项目</span>
        <select
          className="select"
          value={lifeValue}
          onChange={(e) => {
            const [workspaceId, projectId] = e.target.value.split("|");
            setPrefs({ life: workspaceId && projectId ? { workspaceId, projectId } : null });
          }}
        >
          <option value="">还没选</option>
          {rows.map((r) => (
            <option key={r.workspaceId + r.detail.project.project_id} value={`${r.workspaceId}|${r.detail.project.project_id}`}>
              {r.workspaceId} / {r.detail.project.name}
            </option>
          ))}
        </select>
        <span className="meta">这个项目显示在「生活」，其余项目都在「工作」。</span>
      </label>

      <div className="field">
        <span>外观</span>
        <div className="segmented" role="tablist" aria-label="外观">
          {THEMES.map((t) => (
            <button
              key={t.value}
              type="button"
              role="tab"
              aria-selected={prefs.theme === t.value}
              onClick={() => setPrefs({ theme: t.value })}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      <NotificationSetting />

      <section className="section">
        <h2 className="h2">工作区</h2>
        {overview.error !== undefined && <ErrorBlock error={overview.error} onRetry={overview.reload} />}
        <div className="list">
          {entries(overview.data).map((entry) => (
            <WorkspaceRow key={entry.workspace.workspace_id} entry={entry} />
          ))}
        </div>
        <div className="actions" style={{ justifyContent: "flex-start" }}>
          <button type="button" className="btn" onClick={() => setAdding(true)}>
            添加项目文件夹…
          </button>
        </div>
      </section>
      {adding && (
        <AddFolderPanel
          onClose={() => setAdding(false)}
          onAdded={(workspace) => {
            setAdding(false);
            toast(`${workspace.workspace_id} 已添加`);
          }}
        />
      )}
    </div>
  );
}
