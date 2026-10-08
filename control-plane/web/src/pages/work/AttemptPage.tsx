import { useState } from "react";
import { QueryView } from "../../components/Status";
import { core, type Attempt, type BuiltinSessionEvent } from "../../lib/api";
import { useApp, useQuery } from "../../lib/app";
import { dateTime, shortId } from "../../lib/format";
import { ATTEMPT_STATUS } from "../../lib/labels";
import { findRun } from "../../lib/overview";
import { Link } from "../../lib/router";

export function attemptPath(workspaceId: string, runId: string, attemptId: string): string {
  return `/work/w/${workspaceId}/runs/${runId}/attempts/${attemptId}`;
}

export function useRunAttempts(workspaceId: string, runId: string) {
  return useQuery(
    `run-attempts:${workspaceId}:${runId}`,
    () => core(workspaceId).listRunAttempts(runId).then((r) => r.data),
    { workspaces: [workspaceId] },
  );
}

/** "第 2 次" counts Attempts of the same node; Attempt.sequence counts the whole Run. */
export function attemptNumber(attempts: ReadonlyArray<Attempt>, attempt: Attempt): number {
  return attempts.filter((a) => a.plan_node_id === attempt.plan_node_id && a.sequence <= attempt.sequence).length;
}

export function attemptTone(status: Attempt["status"]): string {
  if (status === "succeeded") return "ok";
  if (status === "failed" || status === "timed_out") return "bad";
  if (status === "interrupted" || status === "cancelled") return "attn";
  return "";
}

type Payload = Readonly<Record<string, unknown>>;

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function isRecord(value: unknown): value is Payload {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Pretty-print JSON, including JSON carried inside a string. */
function pretty(value: unknown): string {
  if (typeof value === "string") {
    const text = value.trim();
    if (text.startsWith("{") || text.startsWith("[")) {
      try {
        return JSON.stringify(JSON.parse(text), null, 2);
      } catch {
        return value;
      }
    }
    return value;
  }
  return JSON.stringify(value, null, 2);
}

/** One line that says what a tool call was about: its path, command or query. */
function argSummary(args: unknown): string {
  if (!isRecord(args)) return "";
  if (typeof args["patch"] === "string") {
    const files = [...args["patch"].matchAll(/^\*\*\* (?:Update|Add|Delete) File: (.+)$/gm)].map((m) => m[1]);
    if (files.length) return files.join(", ");
  }
  for (const key of ["path", "command", "cmd", "pattern", "query", "url", "file"]) {
    const value = args[key];
    if (typeof value === "string" && value) return value;
    if (Array.isArray(value) && value.every((v) => typeof v === "string")) return value.join(" ");
  }
  const text = JSON.stringify(args);
  return text.length > 100 ? text.slice(0, 100) + "…" : text;
}

/**
 * A payload over 8KB arrives as { preview, truncated }: the start of its JSON text.
 * Read the call_id and the result's content back out of that prefix.
 */
function previewOf(payload: Payload): string | null {
  return payload["truncated"] === true && typeof payload["preview"] === "string" ? payload["preview"] : null;
}

function callId(event: BuiltinSessionEvent): string {
  const preview = previewOf(event.payload);
  if (preview === null) return str(event.payload["call_id"]);
  return /^\{"call_id":"([^"]+)"/.exec(preview)?.[1] ?? "";
}

function previewText(preview: string): string {
  const start = /"content":"/.exec(preview);
  if (!start) return preview;
  const body = preview.slice(start.index + start[0].length).replace(/\.\.\.\[truncated\]$/, "");
  // The cut can land inside an escape sequence; drop up to 5 trailing characters until it parses.
  for (let cut = 0; cut <= 5; cut += 1) {
    try {
      return (JSON.parse(`"${body.slice(0, body.length - cut)}"`) as string) + "\n…";
    } catch {
      // try a shorter prefix
    }
  }
  return preview;
}

/** Multi-line string arguments (patches, scripts) read better as plain text than as escaped JSON. */
function argsText(args: unknown): string {
  if (!isRecord(args)) return pretty(args ?? {});
  const long = Object.entries(args).filter(([, v]) => typeof v === "string" && v.includes("\n"));
  if (long.length === 0) return pretty(args);
  const rest = Object.fromEntries(Object.entries(args).filter(([, v]) => !(typeof v === "string" && v.includes("\n"))));
  const head = Object.keys(rest).length ? pretty(rest) + "\n\n" : "";
  return head + long.map(([k, v]) => `${k}:\n${v as string}`).join("\n\n");
}

/** A tool result is often { content: "..." }; show the text itself when it is. */
function resultText(result: unknown): string {
  if (isRecord(result) && typeof result["content"] === "string" && Object.keys(result).length === 1) {
    return result["content"];
  }
  return pretty(result);
}

type Usage = { input: number; output: number; cost: number; steps: number };

function usageOf(events: ReadonlyArray<BuiltinSessionEvent>): Usage | null {
  let found = false;
  const total: Usage = { input: 0, output: 0, cost: 0, steps: 0 };
  for (const e of events) {
    if (e.type !== "backend/event" || e.payload["type"] !== "usage") continue;
    const u = e.payload["usage"];
    if (!isRecord(u)) continue;
    found = true;
    total.steps += 1;
    total.input += typeof u["input"] === "number" ? u["input"] : 0;
    total.output += typeof u["output"] === "number" ? u["output"] : 0;
    total.cost += typeof u["estimated_cost"] === "number" ? u["estimated_cost"] : 0;
  }
  return found ? total : null;
}

type Item =
  | { kind: "received"; event: BuiltinSessionEvent }
  | { kind: "message"; event: BuiltinSessionEvent }
  | { kind: "tool"; call: BuiltinSessionEvent; result: BuiltinSessionEvent | null }
  | { kind: "step"; event: BuiltinSessionEvent; number: number }
  | { kind: "problem"; event: BuiltinSessionEvent };

/** Pair each tool call with its result or error; drop routine backend bookkeeping. */
function timeline(events: ReadonlyArray<BuiltinSessionEvent>): Item[] {
  const outcomes = new Map<string, BuiltinSessionEvent>();
  for (const e of events) {
    if (e.type === "tool/result" || e.type === "tool/error") {
      const id = callId(e);
      if (id) outcomes.set(id, e);
    }
  }
  const items: Item[] = [];
  const paired = new Set<BuiltinSessionEvent>();
  let steps = 0;
  for (const e of events) {
    switch (e.type) {
      case "message/received":
        items.push({ kind: "received", event: e });
        break;
      case "model/message":
        items.push({ kind: "message", event: e });
        break;
      case "tool/call": {
        const result = outcomes.get(callId(e)) ?? null;
        if (result) paired.add(result);
        items.push({ kind: "tool", call: e, result });
        break;
      }
      case "tool/result":
      case "tool/error":
        // An outcome without its call (the call was cut off) still shows.
        if (!paired.has(e)) items.push({ kind: "problem", event: e });
        break;
      case "step/start":
        steps += 1;
        items.push({ kind: "step", event: e, number: steps });
        break;
      case "backend/error":
        items.push({ kind: "problem", event: e });
        break;
      default:
        break;
    }
  }
  return items;
}

function Truncated({ event }: { event: BuiltinSessionEvent }) {
  return event.payload_truncated ? <span className="tag attn">内容过长，已截断</span> : null;
}

function Received({ event }: { event: BuiltinSessionEvent }) {
  const preview = previewOf(event.payload);
  const text = preview === null ? pretty(event.payload["content"] ?? event.payload) : previewText(preview);
  return (
    <details className="disclosure trace-item" id={`e-${event.sequence}`}>
      <summary>
        <span className="trace-kind">收到</span>
        <span className="grow trace-line">{text.replace(/\s+/g, " ").slice(0, 120)}</span>
        <Truncated event={event} />
      </summary>
      <pre className="code trace-body">{text}</pre>
    </details>
  );
}

function Message({ event }: { event: BuiltinSessionEvent }) {
  return (
    <div className="trace-item trace-message" id={`e-${event.sequence}`}>
      <div className="trace-kind">模型</div>
      <div className="body pre">{str(event.payload["text"]) || pretty(event.payload)}</div>
      <Truncated event={event} />
    </div>
  );
}

function Tool({ call, result }: { call: BuiltinSessionEvent; result: BuiltinSessionEvent | null }) {
  const failed = result?.type === "tool/error";
  const error = failed && isRecord(result.payload["error"]) ? result.payload["error"] : null;
  const writes = call.payload["writes_workspace"] === true;
  return (
    <details className={`disclosure trace-item${failed ? " trace-error" : ""}`} id={`e-${call.sequence}`} open={failed}>
      <summary>
        <span className="trace-kind">工具</span>
        <span className="mono">{str(call.payload["name"]) || "?"}</span>
        <span className="grow trace-line meta mono">{argSummary(call.payload["arguments"])}</span>
        {writes && <span className="tag attn">写入</span>}
        {failed && <span className="tag bad">报错</span>}
        {result === null && <span className="tag">没有结果</span>}
      </summary>
      <div className="trace-body">
        <div className="meta">参数</div>
        <pre className="code">{argsText(call.payload["arguments"])}</pre>
        {result && (
          <>
            <div className={failed ? "meta bad" : "meta"}>{failed ? "报错" : "返回"}</div>
            <pre className="code">
              {error
                ? `${str(error["code"])}\n${str(error["error"]) || pretty(error)}`
                : outcomeText(result.payload)}
            </pre>
            <Truncated event={result} />
          </>
        )}
        <Truncated event={call} />
      </div>
    </details>
  );
}

function outcomeText(payload: Payload): string {
  const preview = previewOf(payload);
  return preview === null ? resultText(payload["result"] ?? payload) : previewText(preview);
}

function isError(event: BuiltinSessionEvent): boolean {
  return event.type === "tool/error" || event.type === "backend/error";
}

/** An error, or a tool outcome whose call is missing. */
function Problem({ event }: { event: BuiltinSessionEvent }) {
  const error = isError(event);
  return (
    <div className={`trace-item${error ? " trace-error" : ""}`} id={`e-${event.sequence}`}>
      <div className={`trace-kind${error ? " bad" : ""}`}>
        {event.type === "backend/error" ? "后端报错" : error ? "工具报错" : "工具返回"}
      </div>
      <pre className="code">{event.type === "tool/result" ? outcomeText(event.payload) : pretty(event.payload)}</pre>
    </div>
  );
}

type Filter = "all" | "messages" | "errors";

function shown(item: Item, filter: Filter): boolean {
  if (filter === "all") return true;
  if (filter === "messages") return item.kind === "message" || item.kind === "received";
  return (item.kind === "problem" && isError(item.event)) || (item.kind === "tool" && item.result?.type === "tool/error");
}

export function AttemptPage({ workspaceId, runId, attemptId }: { workspaceId: string; runId: string; attemptId: string }) {
  const { overview } = useApp();
  const opts = { workspaces: [workspaceId] };
  const trace = useQuery(
    `attempt-trace:${workspaceId}:${attemptId}`,
    () => core(workspaceId).getAttemptTrace(attemptId).then((r) => r.data),
    opts,
  );
  const plan = useQuery(`run-plan:${workspaceId}:${runId}`, () => core(workspaceId).getRunPlan(runId).then((r) => r.data), opts);
  const attempts = useRunAttempts(workspaceId, runId);
  const [filter, setFilter] = useState<Filter>("all");
  const context = findRun(overview.data, workspaceId, runId);
  const attempt = trace.data?.attempt;
  const node = attempt ? plan.data?.nodes.find((n) => n.plan_node_id === attempt.plan_node_id) : undefined;
  const siblings = attempt ? (attempts.data ?? []).filter((a) => a.plan_node_id === attempt.plan_node_id) : [];

  return (
    <div className="content wide">
      <div className="meta">
        <Link to={`/work/w/${workspaceId}/runs/${runId}`}>{context?.goal.objective ?? `Run ${shortId(runId)}`}</Link>
        {" / "}
        <span className="mono">{shortId(attemptId)}</span>
      </div>
      <QueryView query={trace}>
        {(data) => {
          const items = timeline(data.session_events);
          const usage = usageOf(data.session_events);
          const tools = items.filter((i) => i.kind === "tool").length;
          const errors = items.filter((i) => shown(i, "errors")).length;
          const messages = items.filter((i) => i.kind === "message").length;
          const visible = items.filter((i) => shown(i, filter));
          const a = data.attempt;
          return (
            <>
              <div className="page-head">
                <div className="grow">
                  <h1 className="page-title">{node?.title ?? "Attempt"}</h1>
                  <div className="meta">
                    第 {attemptNumber(attempts.data ?? [a], a)} 次 ·{" "}
                    <span className={attemptTone(a.status)}>{ATTEMPT_STATUS[a.status]}</span> ·{" "}
                    {a.started_at ? dateTime(a.started_at) : dateTime(a.created_at)}
                    {a.ended_at ? ` → ${dateTime(a.ended_at)}` : " → 进行中"}
                  </div>
                  {a.outcome_reason && <p className="body muted pre">{a.outcome_reason}</p>}
                </div>
              </div>

              {siblings.length > 1 && (
                <div className="tags" aria-label="同一节点的其他尝试">
                  {siblings.map((s) => (
                    <Link
                      key={s.attempt_id}
                      className={`tag ${attemptTone(s.status)}`}
                      to={attemptPath(workspaceId, runId, s.attempt_id)}
                      aria-current={s.attempt_id === a.attempt_id ? "page" : undefined}
                      style={s.attempt_id === a.attempt_id ? { fontWeight: 600 } : undefined}
                    >
                      第 {attemptNumber(siblings, s)} 次 · {ATTEMPT_STATUS[s.status]}
                    </Link>
                  ))}
                </div>
              )}

              <div className="stats">
                <div className="stat">
                  <strong>{messages}</strong>
                  <span className="meta">模型回复</span>
                </div>
                <div className="stat">
                  <strong>{tools}</strong>
                  <span className="meta">工具调用</span>
                </div>
                <div className="stat">
                  <strong className={errors ? "bad" : undefined}>{errors}</strong>
                  <span className="meta">报错</span>
                </div>
                {usage && (
                  <div className="stat">
                    <strong>{Math.round((usage.input + usage.output) / 1000)}k</strong>
                    <span className="meta">
                      token · 约 ${usage.cost.toFixed(2)}
                    </span>
                  </div>
                )}
              </div>

              {data.session_events_truncated && (
                <div className="banner attn">
                  <div className="meta">事件太多，只显示了前 {data.session_events.length} 条。</div>
                </div>
              )}

              <div className="segmented" role="tablist" aria-label="筛选">
                {(
                  [
                    ["all", `全部 ${items.length}`],
                    ["messages", "只看对话"],
                    ["errors", `只看报错 ${errors}`],
                  ] as const
                ).map(([value, label]) => (
                  <button key={value} type="button" role="tab" aria-selected={filter === value} onClick={() => setFilter(value)}>
                    {label}
                  </button>
                ))}
              </div>

              {data.session_events.length === 0 ? (
                <p className="empty">这次尝试没有会话记录。模拟 Worker 和外部 Worker 不产生会话事件。</p>
              ) : visible.length === 0 ? (
                <p className="empty">{filter === "errors" ? "没有报错。" : "没有内容。"}</p>
              ) : (
                <div className="trace">
                  {visible.map((item) => {
                    switch (item.kind) {
                      case "received":
                        return <Received key={item.event.sequence} event={item.event} />;
                      case "message":
                        return <Message key={item.event.sequence} event={item.event} />;
                      case "tool":
                        return <Tool key={item.call.sequence} call={item.call} result={item.result} />;
                      case "step":
                        return (
                          <div key={item.event.sequence} className="trace-step meta">
                            第 {item.number} 轮
                          </div>
                        );
                      case "problem":
                        return <Problem key={item.event.sequence} event={item.event} />;
                    }
                  })}
                </div>
              )}
            </>
          );
        }}
      </QueryView>
    </div>
  );
}
