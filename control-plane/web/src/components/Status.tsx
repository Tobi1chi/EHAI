import type { ReactNode } from "react";
import { describeError, type RunStatus } from "../lib/api";
import type { QueryState } from "../lib/app";
import { RUN_STATUS } from "../lib/labels";
import { Icon } from "./Icon";

/** Status is carried by icon shape first and colour second (see the token board). */
export function RunStatusIcon({ status, size = 16 }: { status: RunStatus; size?: number }) {
  switch (status) {
    case "running":
      return (
        <span className="muted" title={RUN_STATUS[status]}>
          <Icon name="spin" size={size} className="spin" strokeWidth={2.4} />
        </span>
      );
    case "paused":
      return (
        <span title={RUN_STATUS[status]}>
          <Icon name="pause" size={size} strokeWidth={2.6} />
        </span>
      );
    case "completed":
      return (
        <span className="ok" title={RUN_STATUS[status]}>
          <Icon name="check" size={size} strokeWidth={2.4} />
        </span>
      );
    case "failed":
    case "cancelled":
      return (
        <span className="bad" title={RUN_STATUS[status]}>
          <Icon name="cross" size={size} strokeWidth={2.4} />
        </span>
      );
    default:
      return (
        <span className="meta" title={RUN_STATUS[status]}>
          <Icon name="ring" size={size} />
        </span>
      );
  }
}

export function Spinner({ size = 14 }: { size?: number }) {
  return <Icon name="spin" size={size} className="spin" strokeWidth={2.4} />;
}

/**
 * Loading, error and stale states for one query. Children render whenever data exists, so
 * a failed refresh keeps the last good view and marks it as possibly outdated.
 */
export function QueryView<T>({
  query,
  children,
  empty,
}: {
  query: QueryState<T>;
  children: (data: T) => ReactNode;
  empty?: ReactNode;
}) {
  if (query.data === undefined) {
    if (query.error !== undefined) return <ErrorBlock error={query.error} onRetry={query.reload} />;
    return (
      <div className="empty" role="status">
        <Spinner /> 正在读取…
      </div>
    );
  }
  return (
    <>
      {query.error !== undefined && (
        <ErrorBlock error={query.error} onRetry={query.reload} stale />
      )}
      <div className={query.error !== undefined ? "query stale" : "query"}>{children(query.data) ?? empty}</div>
    </>
  );
}

export function ErrorBlock({
  error,
  onRetry,
  stale = false,
}: {
  error: unknown;
  onRetry?: () => void;
  stale?: boolean;
}) {
  return (
    <div className="banner bad" role="alert">
      <div className="banner-title">
        <span className="bad">
          <Icon name="alert" size={16} strokeWidth={2.2} />
        </span>
        {/* Reads offer a retry; a failed write is retried from its own control. */}
        {stale ? "刷新失败，下面的内容可能已过期" : onRetry ? "读取失败" : "没有完成"}
      </div>
      <div className="meta pre">{describeError(error)}</div>
      {onRetry && (
        <div>
          <button className="btn small" type="button" onClick={onRetry}>
            重试
          </button>
        </div>
      )}
    </div>
  );
}
