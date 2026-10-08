import { useState } from "react";
import { InboxList } from "../../components/InboxList";
import { InboxPanel, type InboxTarget } from "../../components/InboxPanel";
import { ErrorBlock, QueryView, RunStatusIcon, Spinner } from "../../components/Status";
import { useToast } from "../../components/Toast";
import {
  core,
  errorStatus,
  type Intervention,
  type PlanGraph,
  type RunResultDocument,
  type Run,
} from "../../lib/api";
import { useApp, useQuery } from "../../lib/app";
import { ago, dateTime, shortId } from "../../lib/format";
import { ATTEMPT_STATUS, RUN_STATUS } from "../../lib/labels";
import { findRun } from "../../lib/overview";
import { Link } from "../../lib/router";
import { Icon } from "../../components/Icon";
import { attemptNumber, attemptPath, attemptTone, useRunAttempts } from "./AttemptPage";
import { PlanGraphView, type Trail } from "./PlanGraphView";
import { useSubmission } from "../../lib/submit";

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

function RunControls({ workspaceId, run, onDone }: { workspaceId: string; run: Run; onDone: () => void }) {
  const { touch } = useApp();
  const toast = useToast();
  const submission = useSubmission("run-control");
  const [cancelling, setCancelling] = useState(false);
  const [reason, setReason] = useState("");

  async function act(kind: "pause" | "resume" | "cancel") {
    const client = core(workspaceId);
    const result = await submission.run((key) => {
      if (kind === "pause") return client.pauseRun(run.run_id, { idempotency_key: key });
      if (kind === "resume") return client.resumeRun(run.run_id, { idempotency_key: key });
      return client.cancelRun(run.run_id, { idempotency_key: key, reason: reason.trim() || null });
    });
    if (result) {
      toast(kind === "pause" ? "已暂停 Run" : kind === "resume" ? "已恢复 Run" : "已取消 Run");
      setCancelling(false);
      touch(workspaceId);
      onDone();
    }
  }

  if (TERMINAL.has(run.status)) return null;
  return (
    <div className="section">
      <div className="actions" style={{ justifyContent: "flex-start" }}>
        {run.status === "running" && (
          <button type="button" className="btn" disabled={submission.pending} onClick={() => void act("pause")}>
            暂停 Run
          </button>
        )}
        {run.status === "paused" && (
          <button type="button" className="btn primary" disabled={submission.pending} onClick={() => void act("resume")}>
            恢复 Run
          </button>
        )}
        {!cancelling && (
          <button type="button" className="btn danger" disabled={submission.pending} onClick={() => setCancelling(true)}>
            取消 Run
          </button>
        )}
        {submission.pending && <Spinner />}
      </div>
      {cancelling && (
        <div className="card">
          <label className="field">
            <span>取消原因（可选）</span>
            <input className="input" value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          <p className="meta">取消后不能恢复，已经通过检查的成果会保留。</p>
          <div className="actions">
            <button type="button" className="btn" onClick={() => setCancelling(false)}>
              不取消
            </button>
            <button type="button" className="btn primary" disabled={submission.pending} onClick={() => void act("cancel")}>
              确认取消
            </button>
          </div>
        </div>
      )}
      {submission.error !== undefined && <ErrorBlock error={submission.error} />}
    </div>
  );
}

function PlanView({ plan, trail }: { plan: PlanGraph; trail: Trail }) {
  return (
    <div className="section">
      <PlanGraphView plan={plan} trail={trail} />
      {plan.design_document && (
        <details className="disclosure">
          <summary>方案说明</summary>
          <div className="body pre" style={{ marginTop: 8 }}>
            {plan.design_document}
          </div>
        </details>
      )}
    </div>
  );
}

function InterventionsView({ items, trail }: { items: ReadonlyArray<Intervention>; trail: Trail }) {
  if (items.length === 0) return <p className="empty">这个 Run 没有求助过。</p>;
  return (
    <div className="list">
      {[...items].reverse().map((i) => (
        <div key={i.intervention_id} className="row" style={{ alignItems: "flex-start" }}>
          <span className="grow">
            <span className="title">{i.needed || i.reason}</span>
            <span className="meta">
              {i.kind === "external_effects" ? "要改动外部" : "Worker 卡住了"} · {ago(i.occurred_at)} ·{" "}
              {i.status === "open" ? "待回复" : "已回复"} ·{" "}
              <Link to={attemptPath(trail.workspaceId, trail.runId, i.attempt_id)}>查看轨迹</Link>
            </span>
            {i.reason && i.needed && <span className="meta pre">{i.reason}</span>}
            {i.reply && (
              <span className="body muted pre">
                {i.reply.actor}：{i.reply.message}
              </span>
            )}
          </span>
        </div>
      ))}
    </div>
  );
}

function ResultView({ doc }: { doc: RunResultDocument | null }) {
  if (doc === null) return <p className="empty">还没有成果。完成后这里会显示 commit、diff 和检查结果。</p>;
  const result = doc.result;
  const checks = result.check_result?.checks ?? [];
  return (
    <div className="section">
      <dl className="kv">
        <dt>commit</dt>
        <dd className="mono">{result.commit ?? "—"}</dd>
        <dt>基线</dt>
        <dd className="mono">{result.base_commit ?? "—"}</dd>
        <dt>工作区</dt>
        <dd className="mono">{result.workspace ?? result.configured_workspace ?? "—"}</dd>
        {result.diff_path && (
          <>
            <dt>diff</dt>
            <dd className="mono">{result.diff_path}</dd>
          </>
        )}
        <dt>检查</dt>
        <dd>
          {result.check_result === null
            ? "—"
            : result.check_result.passed === null
              ? "未判定"
              : result.check_result.passed
                ? "通过"
                : "未通过"}
        </dd>
      </dl>
      {checks.length > 0 && (
        <div className="list">
          {checks.map((c) => (
            <div key={c.check_run_id} className="row">
              <span className="grow">
                <span className="mono">{c.check_id.slice(0, 8)}</span>
                {c.human_request && <span className="meta pre">{c.human_request.question}</span>}
                {c.human_decision && (
                  <span className="meta">
                    {c.human_decision.actor}：{c.human_decision.comment}
                  </span>
                )}
                {c.failure_reason && <span className="meta bad pre">{c.failure_reason}</span>}
              </span>
              <span className={`tag ${c.result?.passed ? "ok" : c.result ? "bad" : ""}`}>
                {c.result ? (c.result.passed ? "通过" : "未通过") : c.status}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function AttemptsView({ trail, plan }: { trail: Trail; plan: PlanGraph | undefined }) {
  const titles = new Map((plan?.nodes ?? []).map((n) => [n.plan_node_id, n.title]));
  if (trail.attempts.length === 0) return <p className="empty">还没开始执行。</p>;
  return (
    <div className="list">
      {[...trail.attempts].reverse().map((a) => (
        <Link key={a.attempt_id} className="row" to={attemptPath(trail.workspaceId, trail.runId, a.attempt_id)}>
          <span className="grow">
            <span>
              {titles.get(a.plan_node_id) ?? shortId(a.plan_node_id)}
              <span className="meta"> · 第 {attemptNumber(trail.attempts, a)} 次</span>
            </span>
            <span className="meta">
              {a.started_at ? dateTime(a.started_at) : dateTime(a.created_at)}
              {a.ended_at ? ` → ${dateTime(a.ended_at)}` : " → 进行中"}
              {a.outcome_reason ? ` · ${a.outcome_reason}` : ""}
            </span>
          </span>
          <span className={`tag ${attemptTone(a.status)}`}>{ATTEMPT_STATUS[a.status]}</span>
          <span className="meta">
            <Icon name="chevron" size={16} />
          </span>
        </Link>
      ))}
    </div>
  );
}

export function RunPage({ workspaceId, runId }: { workspaceId: string; runId: string }) {
  const { overview } = useApp();
  const [target, setTarget] = useState<InboxTarget | null>(null);
  const opts = { workspaces: [workspaceId] };
  const client = core(workspaceId);
  const run = useQuery(`run:${workspaceId}:${runId}`, () => client.getRun(runId).then((r) => r.data), opts);
  const plan = useQuery(`run-plan:${workspaceId}:${runId}`, () => client.getRunPlan(runId).then((r) => r.data), opts);
  const inbox = useQuery(
    `run-inbox:${workspaceId}:${runId}`,
    () => client.listInbox({ runId }).then((r) => r.data),
    opts,
  );
  const interventions = useQuery(
    `run-interventions:${workspaceId}:${runId}`,
    () => client.getRunInterventions(runId).then((r) => r.data),
    opts,
  );
  const result = useQuery(
    `run-result:${workspaceId}:${runId}`,
    () =>
      client.getRunResult(runId).then(
        (r) => r.data,
        (error: unknown) => {
          // No result yet is a normal state, not a failure.
          if (errorStatus(error) === 404 || errorStatus(error) === 409) return null;
          throw error;
        },
      ),
    opts,
  );
  const attempts = useRunAttempts(workspaceId, runId);
  const trail: Trail = { workspaceId, runId, attempts: attempts.data ?? [] };
  const context = findRun(overview.data, workspaceId, runId);

  return (
    <div className="content wide">
      <div className="page-head">
        <div className="grow">
          <div className="meta">
            {context ? (
              <Link to={`/work/w/${workspaceId}/projects/${context.project.project_id}`}>
                {workspaceId} / {context.project.name}
              </Link>
            ) : (
              workspaceId
            )}{" "}
            · <span className="mono">{shortId(runId)}</span>
          </div>
          <h1 className="page-title">{context?.goal.objective ?? "Run"}</h1>
        </div>
      </div>
      <QueryView query={run}>
        {(data) => (
          <div className="section">
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <RunStatusIcon status={data.status} />
              <strong>{RUN_STATUS[data.status]}</strong>
              <span className="meta">
                {data.started_at ? `${dateTime(data.started_at)} 开始` : `${dateTime(data.created_at)} 创建`}
                {data.ended_at ? ` · ${dateTime(data.ended_at)} 结束` : ""}
              </span>
            </div>
            {data.status_reason && <p className="body muted pre">{data.status_reason}</p>}
            {data.goal_worker_budget && (
              <p className="meta">
                Worker 次数：已用 {data.goal_worker_budget.used} / {data.goal_worker_budget.max_worker_attempts}
              </p>
            )}
            {(data.predecessor_run_id || (data.successor_run_ids?.length ?? 0) > 0) && (
              <p className="meta">
                {data.predecessor_run_id && (
                  <Link to={`/work/w/${workspaceId}/runs/${data.predecessor_run_id}`}>前一个 Run</Link>
                )}
                {data.successor_run_ids?.map((id) => (
                  <span key={id}>
                    {" "}
                    <Link to={`/work/w/${workspaceId}/runs/${id}`}>后继 Run {shortId(id)}</Link>
                  </span>
                ))}
              </p>
            )}
            <RunControls workspaceId={workspaceId} run={data} onDone={run.reload} />
          </div>
        )}
      </QueryView>

      <section className="section">
        <h2 className="h2">待处理</h2>
        <QueryView query={inbox}>
          {(data) => (
            <InboxList
              rows={data.items.filter((i) => i.pending).map((item) => ({ workspaceId, item }))}
              onOpen={setTarget}
              showWorkspace={false}
              empty="这个 Run 没有要你处理的事"
            />
          )}
        </QueryView>
      </section>

      <section className="section">
        <div className="section-head">
          <h2 className="h2 grow">执行图</h2>
          <span className="meta">点节点看轨迹，拖动查看其余部分</span>
        </div>
        <QueryView query={plan}>{(data) => <PlanView plan={data} trail={trail} />}</QueryView>
      </section>

      <div className="split">
        <section className="section">
          <h2 className="h2">成果</h2>
          <QueryView query={result}>{(data) => <ResultView doc={data} />}</QueryView>
        </section>
        <section className="section">
          <h2 className="h2">求助记录</h2>
          <QueryView query={interventions}>{(data) => <InterventionsView items={data} trail={trail} />}</QueryView>
        </section>
      </div>

      <section className="section">
        <div className="section-head">
          <h2 className="h2 grow">轨迹</h2>
          <span className="meta">点一次尝试，看模型和工具的每一步</span>
        </div>
        <QueryView query={attempts}>{() => <AttemptsView trail={trail} plan={plan.data} />}</QueryView>
      </section>

      {target && <InboxPanel target={target} onClose={() => setTarget(null)} />}
    </div>
  );
}
