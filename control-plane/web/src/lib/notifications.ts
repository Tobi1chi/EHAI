import { useEffect, useRef } from "react";
import type { ThreadSummary } from "./api";
import { notify } from "./desktop";
import { usePrefs } from "./prefs";
import { navigate } from "./router";
import type { ThreadGroup } from "./threads";

/** Phases worth interrupting the user for, with the notification wording. */
const NOTIFY: Partial<Record<ThreadSummary["phase"], string>> = {
  needs_attention: "需要你处理",
  awaiting_approval: "方案等你审阅",
  completed: "已完成，可以取成果",
  failed: "执行失败",
};

/**
 * Notify when a thread moves into a phase that needs the user, while the window is in the
 * background. The first read only records the baseline, so opening the app does not replay
 * every thread's current state.
 */
export function useThreadNotifications(groups: ReadonlyArray<ThreadGroup> | undefined): void {
  const { notify: enabled } = usePrefs();
  const seen = useRef<Map<string, ThreadSummary["phase"]> | null>(null);

  useEffect(() => {
    if (groups === undefined) return;
    const previous = seen.current;
    const next = new Map<string, ThreadSummary["phase"]>();
    for (const group of groups) {
      for (const thread of group.threads) {
        const key = `${group.workspaceId}/${thread.goal_id}`;
        next.set(key, thread.phase);
        const wording = NOTIFY[thread.phase];
        const before = previous?.get(key);
        if (previous === null || wording === undefined || before === thread.phase) continue;
        if (!enabled || (document.visibilityState === "visible" && document.hasFocus())) continue;
        notify(`EHAI · ${wording}`, thread.objective, () =>
          navigate(`/work/w/${group.workspaceId}/threads/${thread.goal_id}`),
        );
      }
    }
    seen.current = next;
  }, [groups, enabled]);
}
