import { useEffect, useState } from "react";
import { DecisionCard } from "../../components/DecisionCard";
import { CaptureDecision } from "../../components/InboxPanel";
import { Icon } from "../../components/Icon";
import { ErrorBlock, QueryView } from "../../components/Status";
import { core, errorStatus, type JsonValue, type WorkflowDefinitionRevision, type WorkflowExecutionView, type WorkflowRun } from "../../lib/api";
import { useQuery } from "../../lib/app";
import { ago, dateTime, shortId, when } from "../../lib/format";
import { byNewest } from "../../lib/overview";
import { AGENT_QUERY_STATE, orderedWorkflowFields, RAIL_ACTION, WORKFLOW_STATE } from "../../lib/labels";
import { Link, navigate } from "../../lib/router";
import { useSubmission } from "../../lib/submit";
import { LifeGate, type Life } from "./LifeGate";
import { useWorkflowRuns } from "./data";

const STATUS: Record<WorkflowRun["status"], string> = {
  awaiting_confirmation: "等待确认",
  completed: "已完成",
  rejected: "没有保存",
};

function title(run: WorkflowRun): string {
  if (run.workflow === "life.review") {
    return `回顾 · ${dateTime(run.review?.as_of ?? run.created_at)}`;
  }
  return `录入 · ${run.proposed_tasks.length} 项`;
}

function trigger(run: WorkflowRun): string {
  if (run.routine_snapshot) return `定时「${run.routine_snapshot.name}」`;
  return "手动录入";
}

function Records({ life }: { life: Life }) {
  const fixedRuns = useWorkflowRuns(life);
  const runs = useQuery(`executions:${life.workspaceId}:${life.projectId}`, () => core(life.workspaceId).listWorkflowExecutions(life.projectId).then(r => r.data), { workspaces: [life.workspaceId] });
  return (
    <div className="content">
      <div className="page-head">
        <div className="grow">
          <div className="meta">{life.projectName}</div>
          <h1 className="page-title">记录</h1>
        </div>
      </div>
      <StartWorkflow key={life.workspaceId + life.projectId} life={life} />
      <QueryView query={runs}>
        {(data) =>
          data.length === 0 ? (
            <p className="empty">还没有记录。开始流程、录入待办或定时回顾之后会显示在这里。</p>
          ) : (
            <div className="list">
              {byNewest([...data], (r) => r.created_at).map((run) => {
                const fixed = fixedRuns.data?.find(r => r.workflow_run_id === run.workflow_run_id);
                return (
                <Link key={run.workflow_run_id} className="row" to={`/life/records/${run.workflow_run_id}`}>
                  <span className={run.status === "waiting" ? "attn" : "meta"}>
                    <Icon name={run.definition.name === "life.review" ? "record" : "tasks"} />
                  </span>
                  <span className="grow">
                    <span className="title">{fixed ? title(fixed) : run.definition.name === "life.review" ? "回顾" : run.definition.name === "life.capture" ? "录入" : run.definition.name}</span>
                    <span className="meta">
                      {fixed ? STATUS[fixed.status] : WORKFLOW_STATE[run.status] ?? "状态未知"} · {fixed ? `${trigger(fixed)} · ` : ""}{ago(run.created_at)}
                      {fixed?.review ? ` · ${fixed.review.open_tasks.length} 项没做完` : ""}
                    </span>
                  </span>
                </Link>
              ); })}
            </div>
          )
        }
      </QueryView>
    </div>
  );
}

function FixedRecord({ life, workflowRunId }: { life: Life; workflowRunId: string }) {
  const client = core(life.workspaceId);
  const opts = { workspaces: [life.workspaceId] };
  const run = useQuery(
    `workflow-run:${life.workspaceId}:${workflowRunId}`,
    () => client.getWorkflowRun(workflowRunId).then((r) => r.data),
    opts,
  );
  const execution = useQuery(
    `workflow-execution:${life.workspaceId}:${workflowRunId}`,
    () => client.getWorkflowExecution(workflowRunId).then((r) => r.data),
    opts,
  );
  return (
    <div className="content">
      <div className="meta">
        <Link to="/life/records">记录</Link> / <span className="mono">{shortId(workflowRunId)}</span>
      </div>
      <QueryView query={run}>
        {(data) => (
          <>
            <div className="page-head">
              <div className="grow">
                <h1 className="page-title">{title(data)}</h1>
                <div className="meta">
                  {STATUS[data.status]} · {trigger(data)} · {dateTime(data.created_at)}
                </div>
              </div>
            </div>
            {data.coalesced_occurrences > 0 && (
              <div className="banner attn">
                <div className="meta">关机期间错过了 {data.coalesced_occurrences} 次，这次一起补上了。</div>
              </div>
            )}
            {data.workflow === "life.capture" && (
              <CaptureDecision workspaceId={life.workspaceId} run={data} onDone={run.reload} />
            )}
            {data.review && (
              <>
                <div className="stats">
                  <div className="stat">
                    <strong>{data.review.open_tasks.length}</strong>
                    <span className="meta">没做完</span>
                  </div>
                  <div className="stat">
                    <strong className={data.review.overdue_task_ids.length ? "bad" : undefined}>
                      {data.review.overdue_task_ids.length}
                    </strong>
                    <span className="meta">已过期</span>
                  </div>
                  <div className="stat">
                    <strong>{data.review.done_count}</strong>
                    <span className="meta">已完成</span>
                  </div>
                  <div className="stat">
                    <strong>{data.review.cancelled_count}</strong>
                    <span className="meta">已取消</span>
                  </div>
                </div>
                <section className="section">
                  <div className="section-head">
                    <h2 className="h2">当时没做完的</h2>
                    <span className="meta">之后的改动不会反映在这里</span>
                  </div>
                  <div className="list">
                    {data.review.open_tasks.map((task) => {
                      const overdue = data.review?.overdue_task_ids.includes(task.task_id);
                      return (
                        <div key={task.task_id} className="row">
                          <span className="grow">{task.title}</span>
                          {task.due_at && (
                            <span className={`meta${overdue ? " bad" : ""}`}>{when(task.due_at, new Date(data.review?.as_of ?? Date.now()))}</span>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </section>
              </>
            )}
          </>
        )}
      </QueryView>
      <details className="disclosure">
        <summary>技术细节</summary>
        <QueryView query={execution}>
          {(data) => (
            <>
              <div className="list">
                {data.steps.map((step) => (
                  <div key={step.step_execution_id} className="row" style={{ minHeight: 44 }}>
                    <span className="grow">
                      <span className="mono">
                        {step.contract.name} v{step.contract.version}
                      </span>
                    </span>
                    <span className="tag">{step.status === "completed" ? "完成" : step.status === "waiting" ? "等待" : "跳过"}</span>
                    <span className="meta">{dateTime(step.recorded_at)}</span>
                  </div>
                ))}
              </div>
              <p className="meta mono">
                {workflowRunId} · {data.run.definition.name} v{data.run.definition.version}
              </p>
            </>
          )}
        </QueryView>
      </details>
    </div>
  );
}

function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function text(value: unknown): string { return typeof value === "string" ? value : ""; }
const INPUT_LABEL: Record<string, string> = {
  from: "出发地", to: "目的地", date: "日期", earliest: "最早出发时间", latest: "最晚出发时间", preferences: "偏好",
};

function StartWorkflow({ life }: { life: Life }) {
  const definitions = useQuery(`definitions:${life.workspaceId}:${life.projectId}`,
    () => core(life.workspaceId).listWorkflowDefinitions(life.projectId).then(r => r.data),
    { workspaces: [life.workspaceId] });
  const [chosen, setChosen] = useState("");
  const latest = new Map<string, WorkflowDefinitionRevision>();
  for (const definition of definitions.data ?? []) {
    if ((latest.get(definition.name)?.revision ?? 0) < definition.revision) latest.set(definition.name, definition);
  }
  const choices = [...latest.values()].sort((a, b) => a.name.localeCompare(b.name));
  const selected = choices.find(d => d.definition_id === chosen) ?? choices[0];
  return <section className="section workflow-start">
    <h2 className="h2">开始流程</h2>
    <QueryView query={definitions} plain>{() => choices.length === 0
      ? <p className="meta">这个生活项目还没有保存自定义流程。先通过 CLI 保存定义。</p>
      : <>
        <label className="field"><span>流程</span>
          <select className="select" value={selected?.definition_id ?? ""} onChange={e => setChosen(e.target.value)}>
            {choices.map(d => <option key={d.definition_id} value={d.definition_id}>{d.name} · 第 {d.revision} 版</option>)}
          </select>
        </label>
        {selected && <WorkflowInputs key={selected.definition_id} life={life} definition={selected} />}
      </>}</QueryView>
  </section>;
}

function WorkflowInputs({ life, definition }: { life: Life; definition: WorkflowDefinitionRevision }) {
  const schema = definition.definition.inputs_schema;
  const properties = object(schema.properties);
  const required = Array.isArray(schema.required) ? schema.required : [];
  const [values, setValues] = useState<Record<string, string | boolean>>({});
  const submission = useSubmission("start-custom-workflow");
  const [frozen, setFrozen] = useState<Readonly<Record<string, JsonValue>> | null>(null);
  const fields = orderedWorkflowFields(properties).map(([name, value]) => ({ name, schema: object(value) }));
  const supported = (s: Record<string, unknown>) => ["string", "integer", "number", "boolean"].includes(text(s.type))
    || (s.type === "array" && object(s.items).type === "string");
  const unsupported = fields.some(f => !supported(f.schema));
  function patternHint(s: Record<string, unknown>, value: string): string {
    if (typeof s.pattern !== "string" || !value) return "";
    try { return new RegExp(s.pattern).test(value) ? "" : "格式不符合要求，请检查日期（年-月-日）或时间（时:分）。"; }
    catch { return "流程的格式规则无法在页面校验，请检查定义。"; }
  }
  const locked = submission.pending || frozen !== null;
  async function start() {
    const inputs: Record<string, JsonValue> = {};
    for (const { name, schema: s } of fields) {
      const value = values[name] ?? (s.type === "boolean" ? false : "");
      if (s.type === "boolean") inputs[name] = value === true;
      else if (s.type === "array") {
        if (String(value) !== "" || required.includes(name)) inputs[name] = String(value).split(/[,，]/).map(v => v.trim()).filter(Boolean);
      }
      else if (s.type === "number" || s.type === "integer") {
        if (String(value) !== "") inputs[name] = Number(value);
      } else if (String(value) !== "" || required.includes(name)) inputs[name] = String(value);
    }
    const requestInputs = frozen ?? inputs;
    setFrozen(requestInputs);
    const result = await submission.run(key => core(life.workspaceId).startCustomWorkflow(definition.definition_id,
      { idempotency_key: key, inputs: requestInputs }));
    if (result) navigate(`/life/records/${encodeURIComponent(result.data.run.workflow_run_id)}`);
    else {
      // Keep the exact payload with the key after an unknown outcome.
      const status = errorStatus(submission.lastError());
      if (status !== null && status < 500) setFrozen(null);
    }
  }
  return <form className="section" onSubmit={e => { e.preventDefault(); void start(); }}>
    <p className="meta">{definition.definition.description}</p>
    <fieldset className="workflow-fields" disabled={locked}>
      {fields.map(({ name, schema: s }) => {
        const label = text(s.title) || INPUT_LABEL[name] || name;
        const value = values[name] ?? "";
        const hint = patternHint(s, String(value));
        const options = Array.isArray(s.enum) ? s.enum.filter((v): v is string => typeof v === "string") : null;
        // JSON Schema "required" means present, not non-empty. Blank window/preferences
        // strings are legitimate inputs in rail.trip and must be sent explicitly.
        let needsValue = required.includes(name) && (s.type !== "string" || Number(s.minLength ?? 0) > 0 || !!options);
        if (required.includes(name) && typeof s.pattern === "string") {
          try { needsValue ||= !new RegExp(s.pattern).test(""); } catch { needsValue = true; }
        }
        const update = (v: string | boolean) => setValues(current => ({ ...current, [name]: v }));
        return <label className="field" key={name}>
          <span>{label}{required.includes(name) ? " *" : "（可选）"}</span>
          {!supported(s) ? <span className="meta">此字段类型暂不支持，请使用 CLI 启动。</span>
            : s.type === "boolean" ? <input type="checkbox" checked={value === true} onChange={e => update(e.target.checked)} />
            : options ? <select className="select" value={String(value)} required={required.includes(name)} onChange={e => update(e.target.value)}>
              <option value="">请选择</option>{options.map(o => <option key={o} value={o}>{o}</option>)}</select>
            : <input className="input" type={s.type === "number" || s.type === "integer" ? "number" : "text"}
              value={String(value)} required={needsValue}
              step={s.type === "integer" ? 1 : "any"}
              min={typeof s.minimum === "number" ? s.minimum : undefined}
              max={typeof s.maximum === "number" ? s.maximum : undefined}
              minLength={typeof s.minLength === "number" ? s.minLength : undefined}
              maxLength={typeof s.maxLength === "number" ? s.maxLength : undefined}
              aria-invalid={!!hint} onChange={e => update(e.target.value)} />}
          {s.type === "array" && <span className="meta">多个值用逗号分隔。</span>}
          {typeof s.description === "string" && <span className="meta">{s.description}</span>}
          {hint && <span className="meta bad">{hint}</span>}
        </label>;
      })}
    </fieldset>
    {submission.error !== undefined && <ErrorBlock error={submission.error} plain />}
    {frozen !== null && !submission.pending && <p className="meta">尚未确认启动结果。重试会核对同一次请求，不会重复启动。</p>}
    <div><button className="btn primary" disabled={submission.pending || unsupported || fields.some(f => !!patternHint(f.schema, String(values[f.name] ?? "")))}>
      {submission.pending ? "正在启动…" : frozen !== null ? "重试启动" : "开始"}
    </button></div>
  </form>;
}

function querySummary(inputs: unknown): string {
  const p = object(inputs);
  const route = [text(p.from), text(p.to)].filter(Boolean).join(" → ");
  return [route, text(p.date), text(p.keyword),
    p.from_no ? `站序 ${text(p.from_no)} → ${text(p.to_no)}` : "",
    Array.isArray(p.train_types) && p.train_types.length ? `车型 ${p.train_types.filter(v => typeof v === "string").join("/")}` : "",
    p.available_only === true ? "只看有票" : "",
    text(p.via) ? `经 ${text(p.via)}` : "",
    typeof p.limit === "number" ? `最多 ${p.limit} 个方案` : ""].filter(Boolean).join(" · ").slice(0, 300);
}

function AgentProgress({ data }: { data: WorkflowExecutionView }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return <>{data.invocations.filter(i => i.kind === "agent").map(invocation => {
    const agent = object(invocation.agent);
    const start = Date.parse(text(agent.started_at));
    const end = Date.parse(text(agent.completed_at));
    const seconds = Number.isFinite(start) ? Math.max(0, Math.floor(((Number.isFinite(end) ? end : now) - start) / 1000)) : null;
    const calls = Array.isArray(agent.tool_calls) ? agent.tool_calls : [];
    return <section className="section" key={invocation.invocation_id}>
      <h2 className="h2">Agent 查询 · {WORKFLOW_STATE[invocation.status] ?? "状态未知"}</h2>
      <div className="meta">第 {String(agent.attempt ?? 1)} 次尝试 · {seconds === null ? "尚未开始" : `用时 ${seconds} 秒`}
        {" · "}{text(agent.harness) || "等待调度"} / {text(agent.model) || "模型未记录"}</div>
      <div className="list">{calls.map((value, index) => {
        const call = object(value);
        return <div className="row agent-query" key={index}>
          <div className="grow"><span className="title">{RAIL_ACTION[text(call.action)] ?? "查询"}</span>
            <span className="meta">{querySummary(call.inputs)}</span></div>
          <span className="tag">{AGENT_QUERY_STATE[text(call.status)] ?? WORKFLOW_STATE[text(call.status)] ?? "状态未知"}</span>
          {call.status !== "running" && <span className="meta">{typeof call.duration_ms === "number" ? `${call.duration_ms} 毫秒` : "—"}</span>}
        </div>;
      })}</div>
      {calls.length === 0 && <p className="meta">尚无查询记录。</p>}
      {invocation.error && <p className="meta bad">本次查询未完成，请查看流程结果或服务配置。</p>}
    </section>;
  })}</>;
}

function stepLabel(data: WorkflowExecutionView, nodeId: string): string {
  const definition = object(object(data.run.definition_snapshot).definition);
  const steps = Array.isArray(definition.steps) ? definition.steps.map(object) : [];
  const step = steps.find(s => s.id === nodeId);
  if (step?.type === "agent") return "Agent 查询";
  if (step?.type === "human") return text(step.question) || "人工确认";
  if (step?.type === "end") return `结束 · ${text(step.outcome)}`;
  if (step?.type === "action") return "连接器动作";
  if (step?.type === "condition") return "条件判断";
  if (step?.type === "wait") return "等待";
  return "流程步骤";
}

function Record({ life, workflowRunId }: { life: Life; workflowRunId: string }) {
  const execution = useQuery(`workflow-execution:${life.workspaceId}:${workflowRunId}`,
    () => core(life.workspaceId).getWorkflowExecution(workflowRunId).then(r => r.data),
    { workspaces: [life.workspaceId] });
  return <QueryView query={execution} plain>{data => {
    if (["life.capture", "life.review"].includes(data.run.definition.name)) return <FixedRecord life={life} workflowRunId={workflowRunId} />;
    return <div className="content">
      <Link to="/life/records">记录</Link>
      <h1 className="page-title">{data.run.definition.name}</h1>
      <p className="meta">{data.waits.some(w => w.kind === "agent" && w.status === "waiting") ? "查询中" : WORKFLOW_STATE[data.run.status] ?? "状态未知"} · {dateTime(data.run.created_at)}</p>
      <AgentProgress data={data} />
      <section className="section"><h2 className="h2">流程步骤</h2>
        {data.steps.map(step => <div className="row" key={step.step_execution_id}>
          <span className="grow">{stepLabel(data, step.node_id)}</span><span className="tag">{WORKFLOW_STATE[step.status]}</span>
        </div>)}
      </section>
      {data.waits.filter(w => w.kind === "human" && w.status === "waiting").map(wait =>
        <DecisionCard key={wait.wait_id} target={{ workspaceId: life.workspaceId, kind: "workflow_confirmation", requestId: wait.wait_id }} />)}
      {data.run.status === "failed" && <p className="banner bad">流程未能完成，请检查连接器是否运行、Agent 配置及查询条件后重新开始。</p>}
    </div>;
  }}</QueryView>;
}

export function LifeRecords() {
  return <LifeGate>{(life) => <Records life={life} />}</LifeGate>;
}

export function LifeRecord({ workflowRunId }: { workflowRunId: string }) {
  return <LifeGate>{(life) => <Record life={life} workflowRunId={workflowRunId} />}</LifeGate>;
}
