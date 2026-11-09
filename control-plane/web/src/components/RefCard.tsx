import { useState, type ReactNode } from "react";
import { core, errorStatus, type AssistantRef, type LifeTask } from "../lib/api";
import { useApp, useQuery, type QueryState } from "../lib/app";
import { when } from "../lib/format";
import { dispositionRows, inboxQuestion, INBOX_KIND, LIFE_TASK_STATE, NEXT_ACTION, RUN_STATUS, THREAD_PHASE } from "../lib/labels";
import { useSubmission } from "../lib/submit";
import { href, Link } from "../lib/router";
import { ErrorBlock } from "./Status";
import { InboxList } from "./InboxList";
import { InboxPanel, type InboxTarget } from "./InboxPanel";

function LiveValue<T>({ query, children }: { query: QueryState<T>; children: (data: T) => ReactNode }) {
  // Never leave a stale object's action buttons active after a failed refresh.
  if (query.error !== undefined) return errorStatus(query.error) === 404
    ? <p className="meta">已不存在</p> : <ErrorBlock error={query.error} onRetry={query.reload} plain />;
  if (query.data === undefined) return <p className="meta">正在读取…</p>;
  return <>{children(query.data)}</>;
}

type Props = { reference: AssistantRef };
function ThreadCard({ reference: ref }: Props) {
  const q = useQuery(`ref-thread:${ref.workspace_id}:${ref.object_id}`,
    () => core(ref.workspace_id).getThread(ref.object_id).then(r => r.data),
    { workspaces: [ref.workspace_id] });
  return <LiveValue query={q}>{thread => <>
    <strong>{thread.objective}</strong>
    <p className="meta">{THREAD_PHASE[thread.phase]} · {NEXT_ACTION[thread.next_action.kind]}</p>
    <Link to={`/work/w/${encodeURIComponent(ref.workspace_id)}/threads/${encodeURIComponent(ref.object_id)}`}>
      {thread.phase === "awaiting_approval" ? "去批准" : "查看线程"}
    </Link>
  </>}</LiveValue>;
}

function RunCard({ reference: ref }: Props) {
  const q = useQuery(`ref-run:${ref.workspace_id}:${ref.object_id}`, async () => {
    const client = core(ref.workspace_id);
    const { data: run } = await client.getRun(ref.object_id);
    const { data: plan } = await client.getRunPlan(ref.object_id);
    return { run, plan };
  }, { workspaces: [ref.workspace_id] });
  return <LiveValue query={q}>{({ run, plan }) => <>
    <strong>执行 · {RUN_STATUS[run.status]}</strong>
    <p className="meta">节点已完成 {plan.nodes.filter(n => n.status === "completed").length} / {plan.nodes.length}</p>
    <Link to={runPath(ref)}>查看执行</Link>
  </>}</LiveValue>;
}

function runPath(ref: AssistantRef) {
  return `/work/w/${encodeURIComponent(ref.workspace_id)}/runs/${encodeURIComponent(ref.object_id)}`;
}

function DiffCard({ reference: ref }: Props) {
  const q = useQuery(`ref-diff:${ref.workspace_id}:${ref.object_id}`, async () => {
    const client = core(ref.workspace_id);
    const { data: doc } = await client.getRunResult(ref.object_id);
    if (!doc.result.diff_path && doc.result.diff_artifact_ids.length === 0) return null;
    const { data: integrations } = await client.listRunIntegrations(ref.object_id);
    const latest = integrations.at(-1);
    if (!latest?.diff_path) return { available: false, files: 0, added: 0, removed: 0 };
    const { data: diff } = await client.getIntegrationDiff(ref.object_id, latest.integration_id);
    const lines = diff.content.split("\n");
    return {
      available: !diff.truncated,
      files: lines.filter(line => line.startsWith("diff --git ")).length,
      added: lines.filter(line => line.startsWith("+") && !line.startsWith("+++")).length,
      removed: lines.filter(line => line.startsWith("-") && !line.startsWith("---")).length,
    };
  }, { workspaces: [ref.workspace_id] });
  return <LiveValue query={q}>{stats => stats === null ? <p className="meta">已不存在</p> : <>
    <strong>改动摘要</strong>
    <p className="meta">{stats.available ? `${stats.files} 个文件 · 新增 ${stats.added} 行 · 删除 ${stats.removed} 行` : "完整改动统计暂不可用"}</p>
    <a href={href(runPath(ref)) + "#result"}>查看成果</a>
  </>}</LiveValue>;
}

function InboxCard({ reference: ref }: Props) {
  const [target, setTarget] = useState<InboxTarget | null>(null);
  const q = useQuery(`ref-inbox:${ref.workspace_id}:${ref.inbox_kind ?? ""}:${ref.object_id}`, async () => {
    if (ref.inbox_kind !== null) return core(ref.workspace_id).getInboxItem(ref.inbox_kind, ref.object_id).then(r => r.data.item);
    const { data } = await core(ref.workspace_id).listInbox();
    return data.items.find(item => item.request_id === ref.object_id && (ref.inbox_kind === null || item.kind === ref.inbox_kind)) ?? null;
  }, { workspaces: [ref.workspace_id] });
  return <><LiveValue query={q}>{item => item === null ? <p className="meta">已不存在</p> : <>
    <span className="meta">{INBOX_KIND[item.kind]} · {item.pending ? "待处理" : "已处理"}</span>
    {item.pending ? <InboxList rows={[{ workspaceId: ref.workspace_id, item }]} onOpen={setTarget} showWorkspace={false} /> : <>
      <strong>{inboxQuestion(item)}</strong>
      <dl className="kv">{dispositionRows(item.disposition ?? {}).map(([label, value]) => <div key={label} style={{ display: "contents" }}>
        <dt>{label}</dt><dd>{label === "时间" ? when(value) : value}</dd>
      </div>)}</dl>
    </>}
  </>}</LiveValue>{target && <InboxPanel target={target} onClose={() => { setTarget(null); q.reload(); }} />}</>;
}

function TaskActions({ workspaceId, task, reload }: { workspaceId: string; task: LifeTask; reload: () => void }) {
  const { touch } = useApp();
  const submission = useSubmission("ref-life-task");
  async function toggle() {
    const result = await submission.run(key => core(workspaceId).updateLifeTask(task.task_id, {
      idempotency_key: key, expected_version: task.version, title: task.title,
      due_at: task.due_at, status: task.status === "done" ? "open" : "done",
    }));
    if (result || errorStatus(submission.lastError()) === 409) { touch(workspaceId); reload(); }
  }
  return <>
    <strong>{task.title}</strong>
    <p className="meta">{LIFE_TASK_STATE[task.status]} · {task.due_at ? when(task.due_at) : "未定时间"}</p>
    {task.status !== "cancelled" && <button className="btn small" disabled={submission.pending} onClick={() => void toggle()}>{task.status === "done" ? "重开" : "完成"}</button>}
    {submission.error !== undefined && <ErrorBlock error={submission.error} plain />}
  </>;
}

function LifeTaskCard({ reference: ref }: Props) {
  const q = useQuery(`ref-life-task:${ref.workspace_id}:${ref.object_id}`,
    () => core(ref.workspace_id).getLifeTask(ref.object_id).then(r => r.data),
    { workspaces: [ref.workspace_id] });
  return <LiveValue query={q}>{task => <TaskActions workspaceId={ref.workspace_id} task={task} reload={q.reload} />}</LiveValue>;
}

/** References contain identity only; every render reads current core facts, with live refresh. */
export function RefCard({ reference }: Props) {
  return <div className="ref-card section">
    {reference.kind === "thread" ? <ThreadCard reference={reference} />
      : reference.kind === "run" ? <RunCard reference={reference} />
      : reference.kind === "inbox" ? <InboxCard reference={reference} />
      : reference.kind === "diff" ? <DiffCard reference={reference} />
      : <LifeTaskCard reference={reference} />}
  </div>;
}
