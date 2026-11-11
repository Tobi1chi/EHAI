import { useEffect, useState } from "react";
import {
  core,
  plainError,
  type DecideNoteRequest,
  type InboxAction,
  type InboxItem,
  type InboxKind,
  type WorkflowRun,
} from "../lib/api";
import { useApp, useQuery } from "../lib/app";
import { ago, when } from "../lib/format";
import {
  actionHint,
  actionLabel,
  dispositionRows,
  INBOX_KIND,
  inboxQuestion,
  inboxUnavailable,
  isLifeCapture,
  RUN_STATUS,
  orderedWorkflowFields,
} from "../lib/labels";
import { actorName, usePrefs } from "../lib/prefs";
import { Link, navigate } from "../lib/router";
import { attemptPath } from "../pages/work/AttemptPage";
import { useSubmission } from "../lib/submit";
import { useToast } from "./Toast";
import { StartRunPanel } from "./StartRunPanel";
import { ErrorBlock, QueryView } from "./Status";

export type InboxTarget = { readonly workspaceId: string; readonly kind: InboxKind; readonly requestId: string };

const FIELD_LABEL: Record<string, string> = {
  plan_revision_id: "方案", version: "版本", node_titles: "任务", phase_titles: "阶段",
  completion_contract_id: "验收条件", check_run_id: "检查", node_title: "节点", question: "问题",
  evidence_artifact_ids: "证据", summary: "摘要", note_summary: "便签摘要", kept: "保留", counts: "统计",
  total: "总数", failed: "失败", title: "标题", summary_text: "摘要", link: "链接", url: "链接",
  feed_url: "订阅地址", feed_title: "订阅源", published_at: "发布时间", updated_at: "更新时间",
  decision_choice: "筛选结论", decision_confidence: "置信度", proposed_tasks: "待办",
  due_at: "到期时间", call_id: "外部调用", action: "动作", count: "数量",
  start: "开始", end: "结束", location: "地点", description: "说明", tasks: "待办", events: "日程",
  request: "出行条件", from: "出发地", to: "目的地", date: "日期", earliest: "最早出发",
  latest: "最晚出发", preferences: "偏好", plans: "出行方案", notes: "说明",
};
function fieldLabel(key: string): string { return FIELD_LABEL[key] ?? key.replace(/_/g, " "); }
const INTERNAL_IDS = new Set(["plan_revision_id", "completion_contract_id", "check_run_id", "call_id", "authorization_id", "item_id"]);
function internalField(key: string): boolean { return INTERNAL_IDS.has(key) || key.endsWith("_artifact_ids"); }
function percentage(value: number): string { return `${Math.round(value * 100)}%`; }
function remainingValidity(expiry: string, now: number): string {
  const minutes = (Date.parse(expiry) - now) / 60000;
  if (minutes < 120) return `${Math.ceil(minutes)} 分钟`;
  if (minutes < 1440) return `${Math.ceil(minutes / 60)} 小时`;
  return `${Math.ceil(minutes / 1440)} 天`;
}
function Subject({ value, field }: { value: unknown; field?: string }) {
  if (value === null || value === undefined) return <span className="meta">未设置</span>;
  if (Array.isArray(value)) return <div className="section">
    {value.slice(0, 5).map((entry, i) => <div className="decision-entry" key={i}><Subject value={entry} /></div>)}
    {value.length > 5 && <details><summary>展开其余 {value.length - 5} 项</summary>{value.slice(5).map((entry, i) => <div className="decision-entry" key={i}><Subject value={entry} /></div>)}</details>}
    {value.length === 0 && <span className="meta">无</span>}
  </div>;
  if (typeof value === "object") {
    const entry = value as Record<string, unknown>;
    if (typeof entry.title === "string" && Array.isArray(entry.legs)) {
      const display = (v: unknown) => typeof v === "string" || typeof v === "number" ? String(v) : "待核实";
      return <article className="itinerary section">
        <h3 className="h2">{entry.title}</h3>
        {typeof entry.summary === "string" && <p className="pre">{entry.summary}</p>}
        {entry.legs.map((value: unknown, index: number) => {
          if (!value || typeof value !== "object" || Array.isArray(value)) return null;
          const leg = value as Record<string, unknown>;
          const line = `${display(leg.train)} ${display(leg.from)} ${display(leg.depart)} → ${display(leg.to)} ${display(leg.arrive)} · ${display(leg.seat_class)} ${typeof leg.price_cny === "number" ? `¥${leg.price_cny}` : "票价待核实"} · 余票 ${display(leg.remaining)}`;
          return <div className="itinerary-leg" key={index}>
            <span className="meta">{display(leg.date)}</span>
            {typeof leg.url === "string" && /^https?:\/\//i.test(leg.url)
              ? <a href={leg.url} target="_blank" rel="noreferrer">{line}</a> : <span>{line}</span>}
          </div>;
        })}
        <p>总价 {typeof entry.total_price_cny === "number" ? `¥${entry.total_price_cny}` : "待核实"} · 总时长 {display(entry.total_duration)} · 换乘 {display(entry.transfers)} 次</p>
        {typeof entry.why === "string" && <p className="pre">{entry.why}</p>}
      </article>;
    }
    if (typeof entry.title === "string" && typeof entry.link === "string" && /^https?:\/\//i.test(entry.link)) {
      const published = typeof entry.published_at === "string" ? entry.published_at : typeof entry.updated_at === "string" ? entry.updated_at : null;
      const meta = [typeof entry.feed_title === "string" ? entry.feed_title : null, published ? when(published) : null,
        typeof entry.decision_choice === "string" ? entry.decision_choice : null,
        typeof entry.decision_confidence === "number" ? percentage(entry.decision_confidence) : null].filter(Boolean);
      return <article className="section decision-article">
        <a href={entry.link} target="_blank" rel="noreferrer">{entry.title}</a>
        {meta.length > 0 && <div className="meta">{meta.join(" · ")}</div>}
        {typeof entry.summary === "string" && <p className="pre">{entry.summary}</p>}
      </article>;
    }
    return <dl className="kv">{orderedWorkflowFields(entry).filter(([key, entry]) => !internalField(key) && entry !== null && entry !== undefined).map(([key, entry]) =>
      <div key={key} style={{ display: "contents" }}><dt>{fieldLabel(key)}</dt><dd><Subject value={entry} field={key} /></dd></div>)}</dl>;
  }
  if (typeof value === "boolean") return <span>{value ? "是" : "否"}</span>;
  if (field === "decision_confidence" && typeof value === "number") return <span>{percentage(value)}</span>;
  const text = String(value);
  if (field === "notes") return <p className="pre">{text}</p>;
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(text) && Number.isFinite(Date.parse(text))) return <span>{when(text)}</span>;
  if (/^https?:\/\//i.test(text)) return <a href={text} target="_blank" rel="noreferrer">{text}</a>;
  return <span className="pre">{text}</span>;
}

function PlanDecision({ workspaceId, item, onDone }: { workspaceId: string; item: InboxItem; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const plan = useQuery(`decision-plan:${workspaceId}:${item.request_id}`,
    () => core(workspaceId).getPlanGraph(item.request_id).then(r => r.data), { workspaces: [workspaceId] });
  return <>
    <p className="meta">批准会固定方案及验收条件；开始执行还需确认执行配置。</p>
    {item.owner.goal_id && <Link to={`/work/w/${encodeURIComponent(workspaceId)}/threads/${encodeURIComponent(item.owner.goal_id)}`}>查看完整方案和验收条件</Link>}
    {plan.error !== undefined && <ErrorBlock error={plan.error} onRetry={plan.reload} plain />}
    <button className="btn primary" disabled={!plan.data || plan.error !== undefined} onClick={() => setOpen(true)}>批准并开始</button>
    {open && plan.data && <StartRunPanel workspaceId={workspaceId} plan={plan.data}
      onClose={() => { setOpen(false); onDone(); }}
      onStarted={runId => { setOpen(false); onDone(); navigate(`/work/w/${encodeURIComponent(workspaceId)}/runs/${encodeURIComponent(runId)}`); }} />}
  </>;
}

function StepDecision({ workspaceId, item, onDone }: { workspaceId: string; item: InboxItem; onDone: () => void }) {
  const prefs = usePrefs();
  const [reason, setReason] = useState("");
  const submission = useSubmission("step-decision");
  const after = useAfterWrite(workspaceId, onDone);
  const action = item.actions.find(a => a.operation === "decide-workflow-step" || a.operation === "reconcile-connector-call")!;
  async function decide(decision: "approve" | "reject") {
    const args = action.arguments;
    const result = await submission.run<unknown>(key => action.operation === "reconcile-connector-call"
      ? core(workspaceId).reconcileConnectorCall(str(args["call_id"]), { idempotency_key: key })
      : core(workspaceId).decideWorkflowStep(str(args["workflow_run_id"]), {
        idempotency_key: key, expected_version: Number(args["expected_version"]), decision,
        actor: actorName(prefs), reason: reason.trim() || (decision === "approve" ? "批准" : "拒绝"),
      }));
    if (result) after(action.operation === "reconcile-connector-call" ? "已请求核对，不会重复执行" : "已记录决定");
  }
  return <div className="section">
    <p className="meta">{actionHint(action)}</p>
    {action.operation === "decide-workflow-step" && <label className="field"><span>说明（可选）</span><textarea className="textarea" value={reason} onChange={e => setReason(e.target.value)} /></label>}
    {submission.error !== undefined && <ErrorBlock error={submission.error} plain />}
    <div className="actions">
      {action.operation === "decide-workflow-step" && <button className="btn" disabled={submission.pending} onClick={() => void decide("reject")}>拒绝</button>}
      <button className="btn primary" disabled={submission.pending} onClick={() => void decide("approve")}>{action.operation === "decide-workflow-step" ? "批准" : "核对调用"}</button>
    </div>
  </div>;
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/**
 * Handle one "needs me" item. Each action calls the source operation it names with the
 * arguments the core supplied; the user only fills input_fields. After a write the item and
 * its Run are re-read from the core instead of being updated locally.
 */
export function DecisionCard({ target }: { target: InboxTarget }) {
  const query = useQuery(
    `inbox:${target.workspaceId}:${target.kind}:${target.requestId}`,
    () => core(target.workspaceId).getInboxItem(target.kind, target.requestId).then(r => r.data.item),
    { workspaces: [target.workspaceId] },
  );
  if (query.error !== undefined) return <ErrorBlock error={query.error} onRetry={query.reload} plain />;
  return <QueryView query={query} plain>{item => <InboxBody key={item.request_id} workspaceId={target.workspaceId} item={item} onDone={query.reload} />}</QueryView>;
}

function InboxBody({ workspaceId, item, onDone }: { workspaceId: string; item: InboxItem; onDone: () => void }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const expiry = item.authorization.expires_at;
  const expired = expiry !== null && Date.parse(expiry) <= now;
  const owner = item.owner;
  const runLink = owner.run_id ? `/work/w/${workspaceId}/runs/${owner.run_id}` : null;
  const decided = item.disposition ? dispositionRows(item.disposition) : [];
  return (
    <div className="section decision-card" style={{ gap: 16 }}>
      <span className="meta">{INBOX_KIND[item.kind]}</span>
      <p className="dialog-title pre">{inboxQuestion(item)}</p>
      <section className="section"><h3 className="h2">批准的对象</h3><Subject value={item.authorization.subject} /></section>
      <dl className="kv">
        <dt>上限</dt><dd>{item.authorization.limit ? `${item.authorization.limit.kind === "count" ? "数量" : "金额"}：${item.authorization.limit.value} ${item.authorization.limit.unit ?? ""}` : "未设置上限"}</dd>
        <dt>有效期</dt><dd>{expiry ? expired ? "已过期" : `剩余 ${remainingValidity(expiry, now)}（至 ${when(expiry)}）` : "未设置到期时间"}</dd>
        {owner.goal_objective && (
          <>
            <dt>目标</dt>
            <dd>{owner.goal_objective}</dd>
          </>
        )}
        {owner.node_title && (
          <>
            <dt>节点</dt>
            <dd>
              {owner.node_title}
              {owner.run_id && owner.attempt_id && (
                <>
                  {" · "}
                  <Link to={attemptPath(workspaceId, owner.run_id, owner.attempt_id)}>查看这一步的轨迹</Link>
                </>
              )}
            </dd>
          </>
        )}
        {owner.run_status && (
          <>
            <dt>Run</dt>
            <dd>
              {RUN_STATUS[owner.run_status]}
              {runLink && (
                <>
                  {" · "}
                  <Link to={runLink}>查看 Run</Link>
                </>
              )}
            </dd>
          </>
        )}
        {owner.workflow_run_id && isLifeCapture(item) && (
          <>
            <dt>记录</dt>
            <dd>
              <Link to={`/life/records/${owner.workflow_run_id}`}>查看这次录入</Link>
            </dd>
          </>
        )}
      </dl>
      {/* A capture's evidence is its source text, which the decision form shows as 原文. */}
      {item.evidence_summary && !isLifeCapture(item) && item.kind !== "workflow_confirmation" && item.kind !== "workflow_reconciliation" && (
        <div className="section">
          {item.kind === "intervention" ? <details><summary>执行者的原始说明</summary><div className="body pre">{item.evidence_summary}</div></details> : <>
            <div className="h2">依据</div>
            <div className="body pre">{item.kind === "plan_approval" ? "请核对下方方案中的任务、阶段和验收条件。" : item.evidence_summary}</div>
          </>}
        </div>
      )}
      {item.evidence.length > 0 && runLink && <Link to={owner.attempt_id && owner.run_id
        ? attemptPath(workspaceId, owner.run_id, owner.attempt_id) : runLink}>
        查看 {item.evidence.length} 项证据及所属执行轨迹
      </Link>}
      {!item.pending && (
        <div className="banner">
          <div className="banner-title">已处理</div>
          {item.unavailable_reason && <p className="meta">{inboxUnavailable(item.unavailable_reason)}</p>}
          {decided.length > 0 && (
            <dl className="kv">
              {decided.map(([label, value]) => (
                <div key={label} style={{ display: "contents" }}>
                  <dt>{label}</dt>
                  <dd>{label === "时间" ? when(value) : value}</dd>
                </div>
              ))}
            </dl>
          )}
          {decided.length === 0 && runLink && (
            <div className="meta">
              决定和后续进展记录在 <Link to={runLink}>Run</Link> 上。
            </div>
          )}
        </div>
      )}
      {item.pending && !item.actionable && (
        <div className="banner attn">
          <div className="banner-title">暂时不能在这里处理</div>
          <div className="meta">{inboxUnavailable(item.unavailable_reason)}</div>
        </div>
      )}
      {expired && <p className="banner attn">已过期，不能再批准这项授权。</p>}
      <fieldset className="decision-actions" disabled={expired || !item.pending || !item.actionable}>
        {item.pending && <Actions workspaceId={workspaceId} item={item} onDone={onDone} />}
      </fieldset>
    </div>
  );
}
function Actions({ workspaceId, item, onDone }: { workspaceId: string; item: InboxItem; onDone: () => void }) {
  const ops = new Set(item.actions.map((a) => a.operation));
  if (ops.has("approve-plan")) return <PlanDecision workspaceId={workspaceId} item={item} onDone={onDone} />;
  if (ops.has("decide-workflow-step") || ops.has("reconcile-connector-call")) return <StepDecision workspaceId={workspaceId} item={item} onDone={onDone} />;
  if (ops.has("decide-human-check")) {
    return <HumanCheckForm workspaceId={workspaceId} actions={item.actions} onDone={onDone} />;
  }
  if (ops.has("reply-intervention")) {
    const action = item.actions.find((a) => a.operation === "reply-intervention") as InboxAction;
    return <ReplyForm workspaceId={workspaceId} action={action} onDone={onDone} />;
  }
  if (ops.has("decide-workflow")) {
    const action = item.actions.find((a) => a.operation === "decide-workflow") as InboxAction;
    return <WorkflowDecisionForm workspaceId={workspaceId} action={action} onDone={onDone} />;
  }
  if (ops.has("add-note-message") || ops.has("decide-note")) {
    return <NoteForms workspaceId={workspaceId} actions={item.actions} onDone={onDone} />;
  }
  return (
    <div className="banner">
      <div className="meta">这类事项暂时不能在页面处理，请使用命令行。</div>
    </div>
  );
}

function useAfterWrite(workspaceId: string, onDone: () => void) {
  const { touch } = useApp();
  const toast = useToast();
  return (message: string) => {
    toast(message);
    touch(workspaceId);
    onDone();
  };
}

function HumanCheckForm({
  workspaceId,
  actions,
  onDone,
}: {
  workspaceId: string;
  actions: ReadonlyArray<InboxAction>;
  onDone: () => void;
}) {
  const prefs = usePrefs();
  const [comment, setComment] = useState("");
  const submission = useSubmission("human-check");
  const after = useAfterWrite(workspaceId, onDone);
  const ordered = [...actions].sort((a, b) => order(a) - order(b));

  function order(a: InboxAction): number {
    if (a.arguments["passed"] !== true) return 0;
    return a.arguments["hold"] === true ? 1 : 2;
  }

  async function decide(action: InboxAction) {
    const label = actionLabel(action);
    const args = action.arguments;
    const result = await submission.run((key) =>
      core(workspaceId).decideHumanCheck(str(args["check_run_id"]), {
        idempotency_key: key,
        request_token: str(args["request_token"]),
        passed: args["passed"] === true,
        actor: actorName(prefs),
        // The core requires a comment; an empty box records which button was pressed.
        comment: comment.trim() || label,
        ...(args["hold"] === true ? { hold: true } : {}),
      }),
    );
    if (result) after(args["passed"] === true ? `已${label}` : "已记为不通过");
  }

  return (
    <div className="section">
      <label className="field">
        <span>说明（可选）</span>
        <textarea className="textarea" value={comment} onChange={(e) => setComment(e.target.value)} />
      </label>
      {ordered.map((action) => (
        <p key={actionLabel(action)} className="meta">
          <strong className="muted">{actionLabel(action)}：</strong>
          {actionHint(action)}
        </p>
      ))}
      {submission.error !== undefined && <ErrorBlock plain error={submission.error} />}
      <div className="actions">
        {ordered.map((action) => (
          <button
            key={actionLabel(action)}
            type="button"
            className={`btn${action.arguments["passed"] === true && action.arguments["hold"] !== true ? " primary" : ""}`}
            disabled={submission.pending}
            onClick={() => void decide(action)}
          >
            {actionLabel(action)}
          </button>
        ))}
      </div>
    </div>
  );
}

function ReplyForm({ workspaceId, action, onDone }: { workspaceId: string; action: InboxAction; onDone: () => void }) {
  const prefs = usePrefs();
  const [message, setMessage] = useState("");
  const submission = useSubmission("reply");
  const after = useAfterWrite(workspaceId, onDone);

  async function reply() {
    const args = action.arguments;
    const result = await submission.run((key) =>
      core(workspaceId).replyIntervention(str(args["intervention_id"]), {
        idempotency_key: key,
        request_token: str(args["request_token"]),
        actor: actorName(prefs),
        message: message.trim(),
      }),
    );
    if (result) {
      setMessage("");
      after("已回复");
    }
  }

  return (
    <div className="section">
      <label className="field">
        <span>回复</span>
        <textarea className="textarea" value={message} onChange={(e) => setMessage(e.target.value)} />
      </label>
      <p className="meta">{actionHint(action)}</p>
      {submission.error !== undefined && <ErrorBlock plain error={submission.error} />}
      <div className="actions">
        <button
          type="button"
          className="btn primary"
          disabled={submission.pending || !message.trim()}
          onClick={() => void reply()}
        >
          回复
        </button>
      </div>
    </div>
  );
}

/** The core lists one decide-note action per decision it accepts now; render exactly those. */
function NoteForms({
  workspaceId,
  actions,
  onDone,
}: {
  workspaceId: string;
  actions: ReadonlyArray<InboxAction>;
  onDone: () => void;
}) {
  const prefs = usePrefs();
  const [message, setMessage] = useState("");
  const submission = useSubmission("note");
  const after = useAfterWrite(workspaceId, onDone);
  const discuss = actions.find((a) => a.operation === "add-note-message");
  const decisions = actions.filter((a) => a.operation === "decide-note");

  async function addMessage(action: InboxAction) {
    const result = await submission.run((key) =>
      core(workspaceId).addNoteMessage(str(action.arguments["note_id"]), {
        idempotency_key: key,
        request_token: str(action.arguments["request_token"]),
        actor: actorName(prefs),
        message: message.trim(),
      }),
    );
    if (result) {
      setMessage("");
      after("已留言");
    }
  }

  async function decideNote(action: InboxAction) {
    const args = action.arguments;
    const result = await submission.run((key) =>
      core(workspaceId).decideNote(str(args["note_id"]), {
        idempotency_key: key,
        request_token: str(args["request_token"]),
        actor: actorName(prefs),
        message: message.trim(),
        action: args["action"] as DecideNoteRequest["action"],
        ...(typeof args["passed"] === "boolean" ? { passed: args["passed"] } : {}),
      }),
    );
    if (result) {
      setMessage("");
      after(`已${actionLabel(action)}`);
    }
  }

  return (
    <div className="section">
      <label className="field">
        <span>消息</span>
        <textarea className="textarea" value={message} onChange={(e) => setMessage(e.target.value)} />
      </label>
      {decisions.length > 0 && (
        <ul className="meta" style={{ margin: 0, paddingLeft: 18 }}>
          {decisions.map((a) => (
            <li key={actionLabel(a)}>
              {actionLabel(a)}：{actionHint(a)}
            </li>
          ))}
        </ul>
      )}
      {submission.error !== undefined && <ErrorBlock plain error={submission.error} />}
      <div className="actions" style={{ flexWrap: "wrap" }}>
        {discuss && (
          <button
            type="button"
            className="btn"
            disabled={submission.pending || !message.trim()}
            onClick={() => void addMessage(discuss)}
          >
            只留言
          </button>
        )}
        {decisions.map((a) => (
          <button
            key={actionLabel(a)}
            type="button"
            className={a.arguments["action"] === "resolve" ? "btn" : "btn primary"}
            disabled={submission.pending || !message.trim()}
            onClick={() => void decideNote(a)}
          >
            {actionLabel(a)}
          </button>
        ))}
      </div>
    </div>
  );
}

export function WorkflowDecisionForm({
  workspaceId,
  action,
  onDone,
}: {
  workspaceId: string;
  action: InboxAction;
  onDone: () => void;
}) {
  const workflowRunId = str(action.arguments["workflow_run_id"]);
  const run = useQuery(
    `workflow-run:${workspaceId}:${workflowRunId}`,
    () => core(workspaceId).getWorkflowRun(workflowRunId).then((r) => r.data),
    { workspaces: [workspaceId] },
  );
  if (run.error !== undefined) return <ErrorBlock error={run.error} onRetry={run.reload} plain />;
  return (
    <QueryView query={run} plain>
      {(data) => <CaptureDecision workspaceId={workspaceId} run={data} onDone={onDone} />}
    </QueryView>
  );
}

/** Approve or reject a life.capture proposal; tasks cannot be edited here by design. */
export function CaptureDecision({
  workspaceId,
  run,
  onDone,
}: {
  workspaceId: string;
  run: WorkflowRun;
  onDone: () => void;
}) {
  const prefs = usePrefs();
  const [reason, setReason] = useState("");
  const submission = useSubmission("workflow-decision");
  const after = useAfterWrite(workspaceId, onDone);
  const count = run.proposed_tasks.length;

  async function decide(decision: "approve" | "reject") {
    const label = decision === "approve" ? `保存 ${count} 项` : "不保存";
    const result = await submission.run((key) =>
      core(workspaceId).decideWorkflow(run.workflow_run_id, {
        idempotency_key: key,
        expected_version: run.version,
        decision,
        actor: actorName(prefs),
        reason: reason.trim() || label,
      }),
    );
    if (result) after(decision === "approve" ? `已保存 ${count} 项待办` : "没有保存");
  }

  return (
    <div className="section" style={{ gap: 16 }}>
      {run.source_text && (
        <div className="section">
          <div className="h2">原话</div>
          <div className="body muted pre">{run.source_text}</div>
        </div>
      )}
      <div className="section">
        <div className="h2">要保存的待办 · {count}</div>
        <ul className="list">
          {run.proposed_tasks.map((task, index) => (
            <li key={index} className="row">
              <span className="grow">
                <span className="title">{task.title}</span>
              </span>
              {task.due_at && <span className="meta">{when(task.due_at)}</span>}
            </li>
          ))}
        </ul>
      </div>
      {run.status === "awaiting_confirmation" ? (
        <>
          <p className="meta">这里不能改内容。要改的话，选「不保存」再重新录入。</p>
          <label className="field">
            <span>备注（可选）</span>
            <input className="input" value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          {submission.error !== undefined && (
            <div className="banner bad" role="alert">
              <div className="meta pre">{plainError(submission.error)}</div>
            </div>
          )}
          <div className="actions">
            <button type="button" className="btn" disabled={submission.pending} onClick={() => void decide("reject")}>
              不保存
            </button>
            <button
              type="button"
              className="btn primary"
              disabled={submission.pending}
              onClick={() => void decide("approve")}
            >
              保存 {count} 项
            </button>
          </div>
        </>
      ) : (
        <div className="banner">
          <div className="banner-title">
            {run.status === "rejected" ? "没有保存" : run.decision ? "已保存" : "已直接保存"}
          </div>
          {run.decision && (
            <div className="meta">
              {run.decision.actor} · {ago(run.decision.decided_at)} · {run.decision.reason}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

