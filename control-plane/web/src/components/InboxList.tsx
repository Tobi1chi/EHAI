import { ago } from "../lib/format";
import { INBOX_KIND, inboxQuestion } from "../lib/labels";
import type { InboxRow } from "../lib/overview";
import { Icon } from "./Icon";
import type { InboxTarget } from "./InboxPanel";

export function InboxList({
  rows,
  onOpen,
  showWorkspace = true,
  empty = "没有等待你的事项",
}: {
  rows: ReadonlyArray<InboxRow>;
  onOpen: (target: InboxTarget) => void;
  showWorkspace?: boolean;
  empty?: string;
}) {
  if (rows.length === 0) return <p className="empty">{empty}</p>;
  return (
    <div className="list">
      {rows.map(({ workspaceId, item }) => (
        <button
          key={workspaceId + item.kind + item.request_id}
          type="button"
          className="row"
          onClick={() => onOpen({ workspaceId, kind: item.kind, requestId: item.request_id })}
        >
          <span className="dot" style={item.actionable ? undefined : { background: "var(--text-3)" }} />
          <span className="grow">
            <span className="title">{inboxQuestion(item)}</span>
            <span className="meta">
              {INBOX_KIND[item.kind]}
              {showWorkspace ? ` · ${workspaceId} / ${item.owner.project_name}` : ""}
              {item.owner.node_title ? ` · ${item.owner.node_title}` : ""}
              {item.created_at ? ` · ${ago(item.created_at)}` : ""}
            </span>
            {!item.actionable && item.unavailable_reason && (
              <span className="meta attn">{item.unavailable_reason}</span>
            )}
          </span>
          <span className="meta">
            <Icon name="chevron" size={16} />
          </span>
        </button>
      ))}
    </div>
  );
}
