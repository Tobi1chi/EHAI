import type {
  AttemptStatus,
  InboxAction,
  InboxKind,
  PlanNodeKind,
  PlanNodeStatus,
  RunStatus,
  ThreadSummary,
  WorkspaceDescriptor,
} from "./api";

// Display wording for status values the core returns; the page never derives these states.

export const RUN_STATUS: Record<RunStatus, string> = {
  pending: "待开始",
  running: "运行中",
  paused: "已暂停",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

export const THREAD_PHASE: Record<ThreadSummary["phase"], string> = {
  needs_attention: "需要你处理",
  running: "执行中",
  paused: "已暂停",
  awaiting_approval: "方案待批准",
  planning: "规划中",
  completed: "已完成",
  failed: "失败",
  idle: "未开始",
};

export function phaseTone(phase: ThreadSummary["phase"]): string {
  if (phase === "needs_attention" || phase === "failed") return "bad";
  if (phase === "awaiting_approval" || phase === "paused") return "attn";
  if (phase === "completed") return "ok";
  return "";
}

export const NEXT_ACTION: Record<ThreadSummary["next_action"]["kind"], string> = {
  start_discussion: "开始规划",
  continue_discussion: "继续讨论",
  review_plan: "审查方案",
  handle_inbox: "去处理",
  watch_run: "看执行",
  resume_run: "去恢复",
  review_result: "取得成果",
  none: "",
};

export const NODE_STATUS: Record<PlanNodeStatus, string> = {
  pending: "待执行",
  ready: "可以开始",
  running: "运行中",
  candidate: "待检查",
  verifying: "检查中",
  stalled: "卡住了",
  suspended: "搁置",
  completed: "已完成",
  failed: "失败",
  pruned: "已放弃",
};

export const NODE_KIND: Record<PlanNodeKind, string> = {
  work: "工作",
  fork: "分叉",
  evaluator: "评估",
  merge: "合并",
  reviewer: "审查",
};

export const ATTEMPT_STATUS: Record<AttemptStatus, string> = {
  pending: "待开始",
  running: "运行中",
  succeeded: "成功",
  failed: "失败",
  timed_out: "超时",
  cancelled: "已取消",
  interrupted: "被打断",
};

export const INBOX_KIND: Record<InboxKind, string> = {
  plan_approval: "方案批准",
  intervention: "求助",
  human_check: "人工检查",
  note: "便签",
  workflow_confirmation: "流程确认",
  workflow_reconciliation: "核对外部调用",
};

export const WORKSPACE_STATUS: Record<WorkspaceDescriptor["status"], string> = {
  registered: "未启动",
  starting: "启动中",
  ready: "已启动",
  stopping: "停止中",
  stopped: "已停止",
  failed: "失败",
};

export function actionLabel(action: InboxAction): string {
  const args = action.arguments;
  switch (action.operation) {
    case "approve-plan":
      return "批准并开始";
    case "decide-human-check":
      if (args["hold"] === true) return "通过并暂停";
      return args["passed"] === true ? "通过" : "不通过";
    case "reply-intervention":
      return "回复";
    case "add-note-message":
      return "只留言";
    case "decide-note":
      switch (args["action"]) {
        case "resolve":
          return "关闭便签";
        case "continue":
          return args["passed"] === true ? "检查通过并继续" : args["passed"] === false ? "检查不通过" : "回复原求助";
        case "propose_process":
          return "提一个流程调整";
        case "revise_plan":
          return "修订方案";
        default:
          return "决定";
      }
    case "decide-workflow":
      return "批准或拒绝";
    case "decide-workflow-step":
      return "批准或拒绝";
    case "reconcile-connector-call":
      return "核对调用";
  }
}

export function actionHint(action: InboxAction): string {
  const args = action.arguments;
  switch (action.operation) {
    case "approve-plan":
      return "批准方案及验收条件，开始执行前还需确认配置。";
    case "decide-human-check":
      if (args["hold"] === true) {
        return "先算通过，但让 Run 停下来，你点恢复后才继续往下做。";
      }
      return args["passed"] === true
        ? "这一版没问题，继续往下做。"
        : "这一版不行，按规则返工或结束。";
    case "reply-intervention":
      return "回复后这一步会接着做；如果 Run 已暂停，还要再点恢复。";
    case "add-note-message":
      return "只留言，不影响执行。";
    case "decide-note":
      switch (args["action"]) {
        case "resolve":
          return "只关掉讨论，不影响执行和批准。";
        case "continue":
          return args["passed"] === undefined
            ? "把你的消息当作原求助的回复，那一步会接着做。"
            : "按你选的结果判定原来的人工检查，消息作为说明。";
        case "propose_process":
          return "让 Planner 起草流程调整，草稿还要审查和应用。";
        case "revise_plan":
          return "带着便签内容去讨论方案，改出来的方案还要另外批准。";
        default:
          return "";
      }
    case "decide-workflow":
      return "保存这些待办，或者不保存。";
    case "decide-workflow-step":
      return "流程按你的决定走批准或拒绝的那条路。";
    case "reconcile-connector-call":
      return "让连接器核对这次调用是否已生效，不会再执行一次。";
  }
}

export const ROUTING_REASON: Record<string, string> = {
  forced_or_empty_catalog: "没有合适的配方",
  explicit_binding: "按指定配方处理",
  unapproved_binding: "指定的配方未批准",
  jev_call_failed_or_unknown: "Jev 调用失败或结果未知",
  invalid_jev_result: "Jev 返回的结果无效",
  invalid_choice_set: "Jev 返回的选项与目录不符",
  jev_escalated: "Jev 判断需要慢环",
  uncertain_judgement: "Jev 拿不准",
  approved_recipe_selected: "用了已发布的配方",
  recipe_paused_before_execution: "配方刚被暂停",
  read_query_failed: "查询失败",
  feedback_misroute: "你标记了答错",
  feedback_execution_failed: "你标记了结果不对",
};

export function routingReason(reason: string | null): string {
  if (!reason) return "";
  return ROUTING_REASON[reason] ?? reason;
}

export const RECIPE_TARGET: Record<string, string> = {
  "life.tasks.list": "查看生活待办",
  "inbox.list": "查看待处理",
};

// A life.capture confirmation is requested on the workflow run itself; a custom workflow's human
// step is requested on its wait record, so its request_id differs from the run's ID.
export function isLifeCapture(item: {
  kind: InboxKind;
  request_id: string;
  owner: { workflow_run_id: string | null };
}): boolean {
  return item.kind === "workflow_confirmation" && item.request_id === item.owner.workflow_run_id;
}

// Life capture confirmations carry a fixed English question written for API callers; the page
// shows the candidates itself. Other questions come from the user's own criteria, a workflow
// definition or the Worker. The caller-facing next_step is not shown: each action button carries
// its own hint.
export function inboxQuestion(item: {
  kind: InboxKind;
  request_id: string;
  question: string;
  owner: { workflow_run_id: string | null };
}): string {
  if (item.kind === "plan_approval") return item.question.replace(/^Approve plan version (\d+)$/, "批准第 $1 版方案");
  if (item.kind === "workflow_reconciliation") return "请核对这次外部调用是否已经生效";
  if (item.question === "Decide this workflow step") return "请确认这一步的内容";
  return isLifeCapture(item) ? "确认后保存这些待办" : item.question;
}

export function inboxUnavailable(reason: string | null): string {
  if (reason === "Authorization window has expired") return "授权已过期。";
  if (reason === "Plan is already approved") return "方案已批准。";
  if (reason === "A newer plan version exists") return "已有新版方案，请处理最新版本。";
  if (reason?.includes("already complete")) return "这项决定已处理。";
  if (reason?.includes("no longer waiting")) return "流程已不再等待这项决定。";
  return "当前状态暂时不能处理，请刷新或查看所属线程。";
}

const DISPOSITION_LABEL: Record<string, string> = {
  decision: "决定",
  disposition: "处置",
  actor: "由",
  reason: "理由",
  decided_at: "时间",
};

const DISPOSITION_VALUE: Record<string, string> = {
  approve: "批准",
  reject: "拒绝",
  resolved: "已解决",
  superseded: "已被新计划取代",
};

export function dispositionRows(disposition: Record<string, unknown>): Array<[string, string]> {
  return Object.entries(DISPOSITION_LABEL).flatMap(([key, label]): Array<[string, string]> => {
    const value = disposition[key];
    if (typeof value !== "string" || value === "") return [];
    const display = DISPOSITION_VALUE[value] ?? (
      key === "reason" && !/[\u3400-\u9fff]/.test(value) ? "已记录处理说明。" : value
    );
    return [[label, display]];
  });
}

export const LIFE_TASK_STATE: Record<string, string> = {
  overdue: "已过期", open: "未完成", done: "已完成", cancelled: "已取消",
};
export function attentionState(kind: string, state: string): string {
  const labels: Readonly<Record<string, string>> = kind === "thread" ? THREAD_PHASE : kind === "inbox" ? INBOX_KIND : LIFE_TASK_STATE;
  return labels[state] ?? "状态未知";
}
export function attentionNote(note: string): string {
  if (note === "life binding not set") return "还没选择生活项目，可在设置中选择。";
  if (note.startsWith("life project not found in workspace ")) return "找不到生活项目，请在设置中重新选择。";
  return "部分队列信息暂时不可用，请稍后刷新。";
}

/** Never expose runtime exception classes or internal error codes in the conversation. */
export function assistantIssue(reason: string | null, unavailable = false): string {
  const value = (reason ?? "").toLowerCase();
  if (value.includes("interrupted by restart")) return "服务重启中断了这次回答，请重新提问。";
  if (value.includes("reply format")) return "这次回答没有按要求整理好，请重新提问。";
  if (value.includes("local-home")) return "当前服务未启用本地助手，请通过本地启动入口打开。";
  if (value.includes("configuration") || value.includes("config")) return "助手配置暂时读不到，请检查本地初始化设置。";
  if (value.includes("timeout") || value.includes("timed out")) return "这次等待回答超时了，请稍后重新提问。";
  if (value.includes("connect") || value.includes("network")) return "暂时连不上助手服务，请检查连接后重新提问。";
  if (value.includes("auth") || value.includes("api key")) return "助手服务暂时无法验证连接，请检查模型连接设置。";
  return unavailable ? "助手暂不可用，请检查本地服务和模型设置。" : "这次未能取得回答，请检查助手服务后重新提问。";
}
