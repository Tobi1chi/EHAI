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
  ready: "就绪",
  running: "运行中",
  candidate: "待检查",
  verifying: "检查中",
  stalled: "停滞",
  suspended: "已挂起",
  completed: "已完成",
  failed: "失败",
  pruned: "已剪除",
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
  interrupted: "中断",
};

export const INBOX_KIND: Record<InboxKind, string> = {
  intervention: "干预",
  human_check: "人工 Gate",
  note: "便签",
  workflow_confirmation: "录入确认",
};

export const WORKSPACE_STATUS: Record<WorkspaceDescriptor["status"], string> = {
  registered: "未启动",
  starting: "启动中",
  ready: "可用",
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
      return "决定";
    case "decide-workflow":
      return "批准或拒绝";
  }
}

export function actionHint(action: InboxAction): string {
  const args = action.arguments;
  switch (action.operation) {
    case "decide-human-check":
      if (args["hold"] === true) {
        return "记录通过，同时暂停 Run，下游工作不会开始；恢复 Run 后 Gate 才结算并继续。";
      }
      return args["passed"] === true
        ? "记录对这一版证据的决定，Gate 由核心判定。"
        : "记录不通过，核心按规则决定返工或失败。";
    case "reply-intervention":
      return "在已批准的边界内回答这个问题；回复会恢复节点，Run 是否需要单独恢复以状态为准。";
    case "add-note-message":
      return "追加一条讨论，不改变执行。";
    case "decide-note":
      return "关闭、继续原请求或起草调整；批准仍然单独进行。";
    case "decide-workflow":
      return "批准后保存这些待办，拒绝则不保存；不会执行待办本身。";
  }
}

export const ROUTING_REASON: Record<string, string> = {
  forced_or_empty_catalog: "没有可用配方，或指定走慢环",
  explicit_binding: "按指定配方处理",
  unapproved_binding: "指定的配方未批准",
  jev_call_failed_or_unknown: "Jev 调用失败或结果未知",
  invalid_jev_result: "Jev 返回的结果无效",
  invalid_choice_set: "Jev 返回的选项与目录不符",
  jev_escalated: "Jev 判断需要慢环",
  uncertain_judgement: "置信度不够",
  approved_recipe_selected: "选中已批准配方",
  recipe_paused_before_execution: "配方在执行前被暂停",
  read_query_failed: "查询失败",
  feedback_misroute: "已标记走错，交回慢环",
  feedback_execution_failed: "已标记执行失败，交回慢环",
};

export function routingReason(reason: string | null): string {
  if (!reason) return "";
  return ROUTING_REASON[reason] ?? reason;
}

export const RECIPE_TARGET: Record<string, string> = {
  "life.tasks.list": "查看生活待办",
  "inbox.list": "查看需要我处理",
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
