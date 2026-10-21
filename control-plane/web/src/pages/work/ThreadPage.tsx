import { useRef, useState, type ReactNode } from "react";
import { InboxPanel, type InboxTarget } from "../../components/InboxPanel";
import { IntegratePanel } from "../../components/IntegratePanel";
import { StartRunPanel } from "../../components/StartRunPanel";
import { ErrorBlock, QueryView, RunStatusIcon, Spinner } from "../../components/Status";
import {
  core,
  type InboxKind,
  type PlanGraph,
  type PlanningConversation,
  type PlanningTurn,
  type ThreadSummary,
  type ThreadTimelineItem,
} from "../../lib/api";
import { useApp, useQuery } from "../../lib/app";
import { ago, dateTime } from "../../lib/format";
import { INBOX_KIND, NEXT_ACTION, RUN_STATUS, THREAD_PHASE, phaseTone } from "../../lib/labels";
import { Link } from "../../lib/router";
import { dismissDiscussion, sendDiscussion, usePendingDiscussion } from "./discussion";

const TIMELINE_PAGE = 500;
const TIMELINE_MAX_PAGES = 10;

type Refs = Readonly<Record<string, unknown>>;

function ref(refs: Refs, name: string): string | null {
  const value = refs[name];
  return typeof value === "string" ? value : null;
}

function runPath(workspaceId: string, runId: string): string {
  return `/work/w/${workspaceId}/runs/${runId}`;
}

async function readTimeline(workspaceId: string, goalId: string): Promise<ThreadTimelineItem[]> {
  const client = core(workspaceId);
  const items: ThreadTimelineItem[] = [];
  let after: number | undefined;
  for (let page = 0; page < TIMELINE_MAX_PAGES; page += 1) {
    const { data } = await client.getThreadTimeline(
      goalId,
      after === undefined ? { limit: TIMELINE_PAGE } : { limit: TIMELINE_PAGE, after },
    );
    items.push(...data.items);
    if (data.next_after === null) break;
    after = data.next_after;
  }
  return items;
}

/** Ordered, de-duplicated conversation ids named by the timeline. */
function conversationIds(items: ReadonlyArray<ThreadTimelineItem>): string[] {
  const ids: string[] = [];
  for (const item of items) {
    const id = item.kind === "discussion_message" ? ref(item.refs, "conversation_id") : null;
    if (id !== null && !ids.includes(id)) ids.push(id);
  }
  return ids;
}

function Bubble({ mine, children }: { mine: boolean; children: ReactNode }) {
  return <div className={mine ? "bubble mine" : "bubble"}>{children}</div>;
}

function TurnView({ turn }: { turn: PlanningTurn }) {
  return (
    <>
      <Bubble mine>{turn.message}</Bubble>
      {turn.status === "running" ? (
        <Bubble mine={false}>
          <span className="meta">
            <Spinner /> Planner 正在调查和思考…
          </span>
        </Bubble>
      ) : turn.status === "failed" ? (
        <div className="banner bad">
          <div className="banner-title">这一轮规划没有完成</div>
          <div className="meta pre">{turn.error ?? "Planner 没有给出原因。"}</div>
        </div>
      ) : (
        turn.reply && <Bubble mine={false}>{turn.reply}</Bubble>
      )}
    </>
  );
}

function PlanCard({
  workspaceId,
  planRevisionId,
  canStart,
  onStart,
}: {
  workspaceId: string;
  planRevisionId: string;
  canStart: boolean;
  onStart: (plan: PlanGraph) => void;
}) {
  const [open, setOpen] = useState(false);
  const plan = useQuery(
    `plan:${workspaceId}:${planRevisionId}`,
    () => core(workspaceId).getPlanGraph(planRevisionId).then((r) => r.data),
    { workspaces: [workspaceId] },
  );
  return (
    <div className="card thread-card">
      <QueryView query={plan}>
        {(graph) => {
          const titles = new Map(graph.nodes.map((n) => [n.plan_node_id, n.title]));
          return (
            <>
              <div className="section-head">
                <div className="grow">
                  <div className="title">方案第 {graph.version} 版</div>
                  <div className="meta">
                    {graph.status === "draft" ? "草稿，等你审查" : "已批准"} · {graph.phases.length} 个阶段 · {graph.nodes.length} 个任务
                  </div>
                </div>
                <button type="button" className="btn small ghost" onClick={() => setOpen((v) => !v)}>
                  {open ? "收起" : "看任务"}
                </button>
                {canStart && (
                  <button type="button" className="btn small primary" onClick={() => onStart(graph)}>
                    {graph.status === "draft" ? "批准并开始" : "开始执行"}
                  </button>
                )}
              </div>
              {open && (
                <div className="section" style={{ gap: 10 }}>
                  {graph.design_document && <div className="meta pre">{graph.design_document}</div>}
                  {graph.phases.map((phase, index) => (
                    <div key={phase.phase_id}>
                      <div className="title">
                        阶段 {index + 1} · {phase.title}
                      </div>
                      <ul className="plan-nodes">
                        {phase.node_ids.map((id) => (
                          <li key={id}>{titles.get(id) ?? id}</li>
                        ))}
                      </ul>
                    </div>
                  ))}
                </div>
              )}
            </>
          );
        }}
      </QueryView>
    </div>
  );
}

/** Wording for a timeline entry from its kind and refs; the core's summary is English for the CLI. */
function eventText(item: ThreadTimelineItem): string {
  const refs = item.refs as Refs;
  const kind = ref(refs, "kind");
  const inbox = kind !== null && kind in INBOX_KIND ? INBOX_KIND[kind as InboxKind] : "待办";
  switch (item.kind) {
    case "goal_created":
      return "创建了线程";
    case "plan_revision_created":
      return "Planner 提出了新的方案";
    case "plan_approved":
      return "批准了方案";
    case "run_started":
      return "开始执行";
    case "run_status_changed": {
      const status = ref(refs, "status");
      return status !== null && status in RUN_STATUS ? `Run ${RUN_STATUS[status as keyof typeof RUN_STATUS]}` : "Run 状态变化";
    }
    case "node_completed":
      return "一个任务通过了检查";
    case "inbox_opened":
      return kind === "human_check" ? item.summary : `${inbox}：需要你处理`;
    case "inbox_resolved":
      return `${inbox}已处理`;
    case "succession_started":
      return "开始了后继 Run";
    case "integration_created":
      return ref(refs, "status") === "conflicted" ? "整合代码成果时有冲突" : "代码成果已整合";
    case "discussion_message":
      return item.summary;
  }
}

function EventLine({ item, children }: { item: ThreadTimelineItem; children?: ReactNode }) {
  return (
    <div className="thread-event">
      <span className="dot" />
      <span className="grow">
        {children ?? eventText(item)}
        <span className="meta"> · {ago(item.at)}</span>
      </span>
    </div>
  );
}

function NextActionButton({
  workspaceId,
  thread,
  onFocusComposer,
  onStartPlan,
  onInbox,
  onIntegrate,
}: {
  workspaceId: string;
  thread: ThreadSummary;
  onFocusComposer: () => void;
  onStartPlan: (planRevisionId: string) => void;
  onInbox: (target: InboxTarget) => void;
  onIntegrate: (runId: string) => void;
}) {
  const action = thread.next_action;
  const refs = action.refs as Refs;
  const label = NEXT_ACTION[action.kind];
  const runId = ref(refs, "run_id");
  switch (action.kind) {
    case "start_discussion":
    case "continue_discussion":
      return (
        <button type="button" className="btn primary" onClick={onFocusComposer}>
          {label}
        </button>
      );
    case "review_plan": {
      const plan = ref(refs, "plan_revision_id");
      return plan === null ? null : (
        <button type="button" className="btn primary" onClick={() => onStartPlan(plan)}>
          {label}
        </button>
      );
    }
    case "handle_inbox": {
      const kind = ref(refs, "kind");
      const requestId = ref(refs, "request_id");
      return kind === null || requestId === null ? null : (
        <button
          type="button"
          className="btn primary"
          onClick={() => onInbox({ workspaceId, kind: kind as InboxKind, requestId })}
        >
          {label}
        </button>
      );
    }
    case "watch_run":
    case "resume_run":
      return runId === null ? null : <Link className="btn primary" to={runPath(workspaceId, runId)}>{label}</Link>;
    case "review_result":
      return runId === null ? null : (
        <button type="button" className="btn primary" onClick={() => onIntegrate(runId)}>
          {label}
        </button>
      );
    case "none":
      return null;
  }
}

export function ThreadPage({ workspaceId, goalId }: { workspaceId: string; goalId: string }) {
  const { touch } = useApp();
  const composer = useRef<HTMLTextAreaElement>(null);
  const [message, setMessage] = useState("");
  const [inbox, setInbox] = useState<InboxTarget | null>(null);
  const [starting, setStarting] = useState<PlanGraph | null>(null);
  const [integrating, setIntegrating] = useState<{ runId: string; integrationId?: string } | null>(null);
  const pending = usePendingDiscussion(workspaceId, goalId);

  const thread = useQuery(
    `thread:${workspaceId}:${goalId}`,
    () => core(workspaceId).getThread(goalId).then((r) => r.data),
    { workspaces: [workspaceId] },
  );
  const timeline = useQuery(`thread-timeline:${workspaceId}:${goalId}`, () => readTimeline(workspaceId, goalId), {
    workspaces: [workspaceId],
  });
  const ids = conversationIds(timeline.data ?? []);
  const conversations = useQuery(
    ids.length ? `thread-conversations:${workspaceId}:${ids.join(",")}` : null,
    () => Promise.all(ids.map((id) => core(workspaceId).getPlanningConversation(id).then((r) => r.data))),
    { workspaces: [workspaceId] },
  );

  const turns = new Map<string, PlanningTurn>();
  for (const conversation of conversations.data ?? ([] as PlanningConversation[])) {
    for (const turn of conversation.turns) turns.set(turn.turn_id, turn);
  }
  const lastConversation = ids.at(-1) ?? null;
  const running = [...turns.values()].some((t) => t.status === "running");
  // Once the core shows a turn for the message, the core's turn (and its failure) is the record;
  // the local copy only covers a send the core has not recorded, so it can be retried.
  const pendingShown =
    pending !== undefined && ![...turns.values()].some((t) => t.message === pending.message);

  async function openPlan(planRevisionId: string) {
    const plan = await core(workspaceId).getPlanGraph(planRevisionId);
    setStarting(plan.data);
  }

  function send() {
    const text = message.trim();
    if (!text) return;
    setMessage("");
    sendDiscussion(workspaceId, goalId, lastConversation, text, () => touch(workspaceId));
  }

  // Node titles for completed-task entries, from each Run's current plan.
  const runIds = [
    ...new Set(
      (timeline.data ?? [])
        .filter((i) => i.kind === "node_completed")
        .map((i) => ref(i.refs as Refs, "run_id"))
        .filter((id): id is string => id !== null),
    ),
  ];
  const runPlans = useQuery(
    runIds.length ? `thread-run-plans:${workspaceId}:${runIds.join(",")}` : null,
    () => Promise.all(runIds.map((id) => core(workspaceId).getRunPlan(id).then((r) => r.data))),
    { workspaces: [workspaceId] },
  );
  const nodeTitles = new Map(
    (runPlans.data ?? []).flatMap((plan) => plan.nodes.map((n) => [n.plan_node_id, n.title] as const)),
  );
  const resolved = new Set(
    (timeline.data ?? []).filter((i) => i.kind === "inbox_resolved").map((i) => ref(i.refs as Refs, "request_id")),
  );
  const latestPlan = thread.data?.plan_revision_id ?? null;
  const canStartLatest = thread.data?.phase === "awaiting_approval";
  const shownTurns = new Set<string>();
  const shownPlans = new Set<string>();

  return (
    <div className="content thread">
      <QueryView query={thread}>
        {(summary) => (
          <div className="page-head">
            <div className="grow">
              <div className="meta">
                <Link to="/work">工作</Link> / {summary.project_name} <span className="mono">{workspaceId}</span>
              </div>
              <h1 className="page-title">{summary.objective}</h1>
              <div className="meta">
                <span className={`tag ${phaseTone(summary.phase)}`}>{THREAD_PHASE[summary.phase]}</span>
                {summary.run_status && ` · 最新 Run ${RUN_STATUS[summary.run_status]}`} · 更新于 {ago(summary.last_activity_at)}
              </div>
            </div>
            <NextActionButton
              workspaceId={workspaceId}
              thread={summary}
              onFocusComposer={() => composer.current?.focus()}
              onStartPlan={(id) => void openPlan(id)}
              onInbox={setInbox}
              onIntegrate={(runId) => setIntegrating({ runId })}
            />
          </div>
        )}
      </QueryView>

      <QueryView query={timeline}>
        {(items) => (
          <div className="thread-timeline">
            {items.map((item) => {
              const refs = item.refs as Refs;
              const key = `${item.kind}:${ref(refs, "event_id") ?? item.at}`;
              const runId = ref(refs, "run_id");
              switch (item.kind) {
                case "discussion_message": {
                  const turnId = ref(refs, "turn_id");
                  const turn = turnId === null ? undefined : turns.get(turnId);
                  if (turnId === null || shownTurns.has(turnId)) return null;
                  shownTurns.add(turnId);
                  const plan = ref(refs, "plan_revision_id") ?? turn?.plan_revision_id ?? null;
                  if (plan !== null) shownPlans.add(plan);
                  return (
                    <div key={key} className="thread-turn">
                      {turn ? <TurnView turn={turn} /> : <Bubble mine>{item.summary}</Bubble>}
                      {plan !== null && (
                        <PlanCard
                          workspaceId={workspaceId}
                          planRevisionId={plan}
                          canStart={canStartLatest && plan === latestPlan}
                          onStart={setStarting}
                        />
                      )}
                    </div>
                  );
                }
                case "plan_revision_created": {
                  const plan = ref(refs, "plan_revision_id");
                  if (plan === null || shownPlans.has(plan)) return null;
                  shownPlans.add(plan);
                  return (
                    <PlanCard
                      key={key}
                      workspaceId={workspaceId}
                      planRevisionId={plan}
                      canStart={canStartLatest && plan === latestPlan}
                      onStart={setStarting}
                    />
                  );
                }
                case "run_started":
                  return runId === null ? null : (
                    <Link key={key} className="row thread-card" to={runPath(workspaceId, runId)}>
                      <RunStatusIcon status={thread.data?.run_id === runId && thread.data.run_status ? thread.data.run_status : "running"} />
                      <span className="grow">
                        <span className="title">开始执行</span>
                        <span className="meta">{dateTime(item.at)} · 打开执行图和轨迹</span>
                      </span>
                    </Link>
                  );
                case "inbox_opened": {
                  const kind = ref(refs, "kind");
                  const requestId = ref(refs, "request_id");
                  const open = requestId !== null && !resolved.has(requestId);
                  return (
                    <div key={key} className={open ? "row thread-card attn" : "row thread-card"}>
                      <span className="grow">
                        <span className="title">{eventText(item)}</span>
                        <span className="meta">
                          {ago(item.at)}
                          {open ? "" : " · 已处理"}
                        </span>
                      </span>
                      {kind !== null && requestId !== null && (
                        <button
                          type="button"
                          className="btn small"
                          onClick={() => setInbox({ workspaceId, kind: kind as InboxKind, requestId })}
                        >
                          查看
                        </button>
                      )}
                    </div>
                  );
                }
                case "node_completed": {
                  const attemptId = ref(refs, "attempt_id");
                  const nodeId = ref(refs, "plan_node_id");
                  const title = nodeId === null ? undefined : nodeTitles.get(nodeId);
                  const text = title === undefined ? eventText(item) : `「${title}」通过了检查`;
                  return (
                    <EventLine key={key} item={item}>
                      {runId !== null && attemptId !== null ? (
                        <Link to={`${runPath(workspaceId, runId)}/attempts/${attemptId}`}>{text}</Link>
                      ) : (
                        text
                      )}
                    </EventLine>
                  );
                }
                case "integration_created": {
                  const integrationId = ref(refs, "integration_id");
                  const conflicted = ref(refs, "status") === "conflicted";
                  return (
                    <div key={key} className={conflicted ? "row thread-card attn" : "row thread-card"}>
                      <span className="grow">
                        <span className="title">{eventText(item)}</span>
                        <span className="meta">
                          {ago(item.at)} · {conflicted ? "冲突留在整合工作区里" : "未改动你的分支"}
                        </span>
                      </span>
                      {runId !== null && integrationId !== null && (
                        <button
                          type="button"
                          className="btn small"
                          onClick={() => setIntegrating({ runId, integrationId })}
                        >
                          {conflicted ? "查看冲突" : "查看差异"}
                        </button>
                      )}
                    </div>
                  );
                }
                default:
                  return <EventLine key={key} item={item} />;
              }
            })}
            {pending !== undefined && pendingShown && (
              <div className="thread-turn">
                <Bubble mine>{pending.message}</Bubble>
                {pending.inFlight ? (
                  <Bubble mine={false}>
                    <span className="meta">
                      <Spinner /> Planner 正在调查和思考…
                    </span>
                  </Bubble>
                ) : (
                  <>
                    <ErrorBlock error={pending.error} />
                    <div className="actions" style={{ justifyContent: "flex-start" }}>
                      <button
                        type="button"
                        className="btn small"
                        onClick={() =>
                          sendDiscussion(
                            workspaceId,
                            goalId,
                            pending.conversationId,
                            pending.message,
                            () => touch(workspaceId),
                            pending,
                          )
                        }
                      >
                        重新发送
                      </button>
                      <button type="button" className="btn small ghost" onClick={() => dismissDiscussion(workspaceId, goalId)}>
                        放弃
                      </button>
                    </div>
                  </>
                )}
              </div>
            )}
          </div>
        )}
      </QueryView>

      <div className="composer sticky-composer">
        <textarea
          ref={composer}
          value={message}
          placeholder={lastConversation ? "继续和 Planner 讨论，或要求修改方案…" : "告诉 Planner 要怎么做…"}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              send();
            }
          }}
        />
        <div className="composer-bar">
          <span className="meta grow">⌘/Ctrl + Enter 发送。讨论不会批准方案或改动代码。</span>
          <button
            type="button"
            className="btn primary small"
            disabled={!message.trim() || running || pending?.inFlight === true}
            onClick={send}
          >
            发送
          </button>
        </div>
      </div>

      {inbox && <InboxPanel target={inbox} onClose={() => setInbox(null)} />}
      {starting && (
        <StartRunPanel
          workspaceId={workspaceId}
          plan={starting}
          onClose={() => setStarting(null)}
          onStarted={() => setStarting(null)}
        />
      )}
      {integrating && (
        <IntegratePanel
          workspaceId={workspaceId}
          runId={integrating.runId}
          {...(integrating.integrationId === undefined ? {} : { integrationId: integrating.integrationId })}
          onClose={() => setIntegrating(null)}
        />
      )}
    </div>
  );
}
