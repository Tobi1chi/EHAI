import { useSyncExternalStore } from "react";
import { core, newIdempotencyKey, outcomeUnknown } from "../../lib/api";

/**
 * Planning messages this page has sent and is still waiting on. A planner turn can take
 * minutes, so the request outlives the page that sent it; the thread page reads this to show
 * the pending message until the core's timeline has it. Nothing here is business state: the
 * core's conversation is the record, and a failed send keeps its key so a retry is the same write.
 */
export type PendingMessage = {
  readonly goalId: string;
  readonly conversationId: string | null;
  readonly message: string;
  readonly key: string;
  readonly error: unknown;
  readonly inFlight: boolean;
};

const pending = new Map<string, PendingMessage>();
const listeners = new Set<() => void>();
let snapshot: ReadonlyMap<string, PendingMessage> = new Map();

function publish() {
  snapshot = new Map(pending);
  for (const listener of listeners) listener();
}

function threadKey(workspaceId: string, goalId: string): string {
  return `${workspaceId}:${goalId}`;
}

export function sendDiscussion(
  workspaceId: string,
  goalId: string,
  conversationId: string | null,
  message: string,
  onSettled: () => void,
  retryOf?: PendingMessage,
): void {
  const id = threadKey(workspaceId, goalId);
  const key = retryOf !== undefined && outcomeUnknown(retryOf.error) ? retryOf.key : newIdempotencyKey("discuss");
  pending.set(id, { goalId, conversationId, message, key, error: undefined, inFlight: true });
  publish();
  core(workspaceId)
    .discussPlan({ idempotency_key: key, goal_id: goalId, message, conversation_id: conversationId })
    .then(
      () => {
        pending.delete(id);
        publish();
        onSettled();
      },
      (error: unknown) => {
        pending.set(id, { goalId, conversationId, message, key, error, inFlight: false });
        publish();
        onSettled();
      },
    );
}

export function dismissDiscussion(workspaceId: string, goalId: string): void {
  pending.delete(threadKey(workspaceId, goalId));
  publish();
}

export function usePendingDiscussion(workspaceId: string, goalId: string): PendingMessage | undefined {
  const all = useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => snapshot,
  );
  return all.get(threadKey(workspaceId, goalId));
}
