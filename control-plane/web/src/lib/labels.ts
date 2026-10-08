import type {
  AttemptStatus,
  InboxAction,
  InboxKind,
  PlanNodeKind,
  PlanNodeStatus,
  RunStatus,
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
  intervention: "求助",
  human_check: "人工检查",
  note: "便签",
  workflow_confirmation: "录入确认",
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
  }
}

export function actionHint(action: InboxAction): string {
  const args = action.arguments;
  switch (action.operation) {
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

// Workflow confirmations carry a fixed English question written for API callers; the page shows
// the candidates itself. Other questions come from the user's own criteria or the Worker.
// The caller-facing next_step is not shown: each action button carries its own hint.
export function inboxQuestion(item: { kind: InboxKind; question: string }): string {
  return item.kind === "workflow_confirmation" ? "确认后保存这些待办" : item.question;
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
    return [[label, DISPOSITION_VALUE[value] ?? value]];
  });
}
