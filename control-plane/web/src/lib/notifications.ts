import { useEffect, useRef } from "react";
import { manager, type AttentionItem } from "./api";
import { useApp, useQuery } from "./app";
import { notify } from "./desktop";
import { usePrefs } from "./prefs";
import { navigate } from "./router";
import { inboxQuestion } from "./labels";

export function queueLink(item: AttentionItem): string {
  const ws = encodeURIComponent(item.workspace_id);
  if (item.kind === "thread") return `/work/w/${ws}/threads/${encodeURIComponent(item.object_id)}`;
  if (item.kind === "life_task") return "/life/tasks";
  if (item.run_id) return `/work/w/${ws}/runs/${encodeURIComponent(item.run_id)}`;
  if (item.kind === "inbox") return `/decisions/${ws}/${encodeURIComponent(item.state)}/${encodeURIComponent(item.object_id)}`;
  return item.label === "life" ? "/life" : "/work";
}
const itemKey = (item: AttentionItem) => `${item.kind}:${item.workspace_id}:${item.object_id}`;

export function useQueueNotifications(): void {
  const { overview } = useApp();
  const queue = useQuery("notification-attention", () => manager.getAttention().then(r => r.data), {
    workspaces: (overview.data?.workspaces ?? []).map(e => e.workspace.workspace_id), intervalMs: 30000,
  });
  const { notify: enabled } = usePrefs();
  const seen = useRef<Map<string, AttentionItem> | null>(null);
  useEffect(() => {
    const data = queue.data;
    if (!data || queue.error !== undefined) return;
    const previous = seen.current;
    const next = new Map(data.needs_you.map(item => [itemKey(item), item]));
    // A temporarily unreadable workspace is not evidence that its rows left the queue.
    for (const [key, item] of previous ?? []) {
      if (data.unavailable.some(entry => entry.workspace_id === item.workspace_id)) next.set(key, item);
    }
    if (previous !== null && enabled && !(document.visibilityState === "visible" && document.hasFocus())) {
      for (const item of data.needs_you) {
        if (previous.has(itemKey(item))) continue;
        const plan = item.state === "awaiting_approval" || item.state === "plan_approval" || item.decisions?.some(d => d.kind === "plan_approval");
        const title = plan ? "方案等你批准" : item.kind === "life_task" ? "生活待办到期" : item.kind === "inbox" && item.state === "workflow_confirmation" ? "待你确认" : "需要你处理";
        const inbox = overview.data?.workspaces.find(entry => entry.workspace.workspace_id === item.workspace_id)?.inbox?.items
          .find(entry => entry.request_id === item.object_id && entry.kind === item.state);
        const body = inbox ? inboxQuestion(inbox) : item.title;
        notify(`EHAI · ${title}`, body, () => navigate(queueLink(item)));
      }
    }
    seen.current = next;
  }, [queue.data, queue.error, enabled, overview.data]);

  useEffect(() => {
    const data = queue.data;
    if (!data || queue.error !== undefined) return;
    const now = Date.now();
    const times = [...data.needs_you, ...data.in_progress].filter(item =>
      item.kind === "life_task" && item.state === "open" && item.at !== null
    ).map(item => Date.parse(item.at!)).filter(at => at > now);
    if (!times.length) return;
    const timer = window.setTimeout(queue.reload, Math.min(86400000, Math.max(1, Math.min(...times) - now + 100)));
    return () => window.clearTimeout(timer);
  }, [queue.data, queue.error, queue.reload]);
}
