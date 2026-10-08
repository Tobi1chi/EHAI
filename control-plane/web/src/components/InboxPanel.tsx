import { useState } from "react";
import {
  core,
  describeError,
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
  RUN_STATUS,
} from "../lib/labels";
import { actorName, usePrefs } from "../lib/prefs";
import { Link } from "../lib/router";
import { attemptPath } from "../pages/work/AttemptPage";
import { useSubmission } from "../lib/submit";
import { useToast } from "./Toast";
import { Panel } from "./Panel";
import { ErrorBlock, QueryView } from "./Status";

export type InboxTarget = { readonly workspaceId: string; readonly kind: InboxKind; readonly requestId: string };

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/**
 * Handle one "needs me" item. Each action calls the source operation it names with the
 * arguments the core supplied; the user only fills input_fields. After a write the item and
 * its Run are re-read from the core instead of being updated locally.
 */
export function InboxPanel({ target, onClose }: { target: InboxTarget; onClose: () => void }) {
  const query = useQuery(
    `inbox:${target.workspaceId}:${target.kind}:${target.requestId}`,
    () => core(target.workspaceId).getInboxItem(target.kind, target.requestId).then((r) => r.data.item),
    { workspaces: [target.workspaceId] },
  );
  const item = query.data;
  return (
    <Panel
      title={item ? INBOX_KIND[item.kind] : "待处理"}
      subtitle={
        item
          ? `${target.workspaceId} / ${item.owner.project_name}${item.created_at ? ` · ${ago(item.created_at)}` : ""}`
          : target.workspaceId
      }
      onClose={onClose}
    >
      <QueryView query={query}>{(data) => <InboxBody workspaceId={target.workspaceId} item={data} onDone={query.reload} />}</QueryView>
    </Panel>
  );
}

function InboxBody({ workspaceId, item, onDone }: { workspaceId: string; item: InboxItem; onDone: () => void }) {
  const owner = item.owner;
  const runLink = owner.run_id ? `/work/w/${workspaceId}/runs/${owner.run_id}` : null;
  const decided = item.disposition ? dispositionRows(item.disposition) : [];
  return (
    <div className="section" style={{ gap: 16 }}>
      <p className="dialog-title pre">{inboxQuestion(item)}</p>
      <dl className="kv">
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
        {owner.workflow_run_id && (
          <>
            <dt>记录</dt>
            <dd>
              <Link to={`/life/records/${owner.workflow_run_id}`}>查看这次录入</Link>
            </dd>
          </>
        )}
      </dl>
      {/* A capture's evidence is its source text, which the decision form shows as 原文. */}
      {item.evidence_summary && item.kind !== "workflow_confirmation" && (
        <div className="section">
          <div className="h2">依据</div>
          <div className="body pre">{item.evidence_summary}</div>
        </div>
      )}
      {item.evidence.length > 0 && (
        <div className="tags">
          {item.evidence.map((e) => (
            <span key={e.artifact_id} className="tag mono" title={e.sha256 ?? undefined}>
              {e.artifact_id.slice(0, 8)}
            </span>
          ))}
        </div>
      )}
      {!item.pending && (
        <div className="banner">
          <div className="banner-title">已处理</div>
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
          <div className="meta">{item.unavailable_reason ?? "可以先用命令行处理。"}</div>
        </div>
      )}
      {item.actionable && <Actions workspaceId={workspaceId} item={item} onDone={onDone} />}
    </div>
  );
}

function Actions({ workspaceId, item, onDone }: { workspaceId: string; item: InboxItem; onDone: () => void }) {
  const ops = new Set(item.actions.map((a) => a.operation));
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
      <div className="meta">这类事项暂时只能用命令行处理（{item.actions.map((a) => a.operation).join("、")}）。</div>
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
      {submission.error !== undefined && <ErrorBlock error={submission.error} />}
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
      {submission.error !== undefined && <ErrorBlock error={submission.error} />}
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
      {submission.error !== undefined && <ErrorBlock error={submission.error} />}
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
  return (
    <QueryView query={run}>
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
              <div className="meta pre">{describeError(submission.error)}</div>
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
