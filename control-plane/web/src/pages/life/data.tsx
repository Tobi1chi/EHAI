import { useState } from "react";
import { Panel } from "../../components/Panel";
import { ErrorBlock } from "../../components/Status";
import { useToast } from "../../components/Toast";
import { core, errorStatus, type LifeTask, type UpdateLifeTaskRequest } from "../../lib/api";
import { useApp, useQuery } from "../../lib/app";
import { dueBucket, hm, localDateInput, when } from "../../lib/format";
import { useSubmission } from "../../lib/submit";
import type { Life } from "./LifeGate";

export function useLifeTasks(life: Life) {
  return useQuery(
    `life-tasks:${life.workspaceId}:${life.projectId}`,
    () => core(life.workspaceId).listLifeTasks(life.projectId).then((r) => r.data),
    { workspaces: [life.workspaceId] },
  );
}

export function useRoutines(life: Life) {
  return useQuery(
    `routines:${life.workspaceId}:${life.projectId}`,
    () => core(life.workspaceId).listRoutines(life.projectId).then((r) => r.data),
    { workspaces: [life.workspaceId] },
  );
}

/** Scheduler liveness is not an event, so it is also polled. */
export function useScheduler(life: Life) {
  return useQuery(
    `scheduler:${life.workspaceId}`,
    () => core(life.workspaceId).getRoutineScheduler().then((r) => r.data),
    { workspaces: [life.workspaceId], intervalMs: 30000 },
  );
}

export function useWorkflowRuns(life: Life) {
  return useQuery(
    `workflow-runs:${life.workspaceId}:${life.projectId}`,
    () => core(life.workspaceId).listWorkflowRuns(life.projectId).then((r) => r.data),
    { workspaces: [life.workspaceId] },
  );
}

/** Text typed on 今天 and carried to 录入 or 问 without putting it in the URL. */
let draft: string | null = null;
export function setDraft(text: string): void {
  draft = text;
}
export function takeDraft(): string {
  const text = draft ?? "";
  draft = null;
  return text;
}

export function sortByDue(tasks: ReadonlyArray<LifeTask>): LifeTask[] {
  return [...tasks].sort((a, b) => {
    // Compare instants: the core keeps each due_at in the offset it was written with.
    if (a.due_at && b.due_at) return Date.parse(a.due_at) - Date.parse(b.due_at);
    if (a.due_at) return -1;
    if (b.due_at) return 1;
    return b.created_at.localeCompare(a.created_at);
  });
}

function request(task: LifeTask, key: string, change: Partial<UpdateLifeTaskRequest>): UpdateLifeTaskRequest {
  return {
    idempotency_key: key,
    expected_version: task.version,
    title: task.title,
    due_at: task.due_at,
    status: task.status,
    ...change,
  };
}

/** Toggle done with an undo that writes the previous status back against the new version. */
export function TaskRow({ life, task, onOpen }: { life: Life; task: LifeTask; onOpen?: (task: LifeTask) => void }) {
  const { touch } = useApp();
  const toast = useToast();
  const submission = useSubmission("task-status");
  // Show the requested status until the list reloads with a newer version of this task.
  const [optimistic, setOptimistic] = useState<{ version: number; status: LifeTask["status"] } | null>(null);
  const status = optimistic?.version === task.version ? optimistic.status : task.status;
  const done = status === "done";
  const overdue = status === "open" && dueBucket(task.due_at) === "overdue";

  async function toggle() {
    const next = done ? "open" : "done";
    setOptimistic({ version: task.version, status: next });
    const result = await submission.run((key) =>
      core(life.workspaceId).updateLifeTask(task.task_id, request(task, key, { status: next })),
    );
    if (!result) {
      setOptimistic(null);
      toast(errorStatus(submission.lastError()) === 409 ? "这条待办刚在别处改过，已刷新" : "没保存上，再试一次");
      touch(life.workspaceId);
      return;
    }
    touch(life.workspaceId);
    if (next === "done") {
      const updated = result.data;
      toast(`已完成「${task.title}」`, {
        label: "撤销",
        run: () => {
          void core(life.workspaceId)
            .updateLifeTask(updated.task_id, request(updated, `ui-undo-${updated.task_id}-${updated.version}`, { status: "open" }))
            .finally(() => touch(life.workspaceId));
        },
      });
    }
  }

  return (
    <div className="row">
      <input
        className="check"
        type="checkbox"
        checked={done}
        disabled={submission.pending || task.status === "cancelled"}
        onChange={() => void toggle()}
        aria-label={`${done ? "取消完成" : "完成"} ${task.title}`}
      />
      {onOpen ? (
        <button
          type="button"
          className="grow"
          style={{ border: 0, background: "none", padding: 0, textAlign: "left", cursor: "pointer" }}
          onClick={() => onOpen(task)}
        >
          <span style={done || task.status === "cancelled" ? { textDecoration: "line-through", color: "var(--text-3)" } : undefined}>
            {task.title}
          </span>
        </button>
      ) : (
        <span className="grow">{task.title}</span>
      )}
      {task.due_at && <span className={`meta${overdue ? " bad" : ""}`}>{when(task.due_at)}</span>}
    </div>
  );
}

function splitDue(iso: string | null): { date: string; time: string } {
  if (!iso) return { date: "", time: "" };
  const d = new Date(iso);
  const time = hm(d);
  return { date: localDateInput(d), time: time === "00:00" ? "" : time };
}

export function joinDue(date: string, time: string): string | null {
  if (!date) return null;
  const d = new Date(`${date}T${time || "00:00"}`);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

export function TaskEditPanel({ life, task, onClose }: { life: Life; task: LifeTask; onClose: () => void }) {
  const { touch } = useApp();
  const toast = useToast();
  const initial = splitDue(task.due_at);
  const [title, setTitle] = useState(task.title);
  const [date, setDate] = useState(initial.date);
  const [time, setTime] = useState(initial.time);
  const [status, setStatus] = useState(task.status);
  const submission = useSubmission("task-edit");
  const conflict = errorStatus(submission.error) === 409;

  async function save() {
    const unchanged =
      title.trim() === task.title && date === initial.date && time === initial.time && status === task.status;
    if (unchanged) {
      onClose();
      return;
    }
    const result = await submission.run((key) =>
      core(life.workspaceId).updateLifeTask(
        task.task_id,
        request(task, key, { title: title.trim(), due_at: joinDue(date, time), status }),
      ),
    );
    if (result) {
      toast("已保存");
      touch(life.workspaceId);
      onClose();
    }
  }

  return (
    <Panel
      title="编辑待办"
      onClose={onClose}
      footer={
        <div className="actions">
          <button type="button" className="btn" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={submission.pending || !title.trim() || conflict}
            onClick={() => void save()}
          >
            保存
          </button>
        </div>
      }
    >
      <label className="field">
        <span>标题</span>
        <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} />
      </label>
      <div className="split">
        <label className="field">
          <span>截止日期</span>
          <input className="input" type="date" value={date} onChange={(e) => setDate(e.target.value)} />
        </label>
        <label className="field">
          <span>时间（可选）</span>
          <input className="input" type="time" value={time} disabled={!date} onChange={(e) => setTime(e.target.value)} />
        </label>
      </div>
      <label className="field">
        <span>状态</span>
        <select className="select" value={status} onChange={(e) => setStatus(e.target.value as LifeTask["status"])}>
          <option value="open">未完成</option>
          <option value="done">已完成</option>
          <option value="cancelled">已取消</option>
        </select>
      </label>
      {conflict ? (
        <div className="banner attn" role="alert">
          <div className="banner-title">这条待办刚在别处改过</div>
          <div className="meta">关掉再重新打开，在最新内容上改，免得覆盖掉。</div>
        </div>
      ) : (
        submission.error !== undefined && <ErrorBlock error={submission.error} />
      )}
    </Panel>
  );
}
