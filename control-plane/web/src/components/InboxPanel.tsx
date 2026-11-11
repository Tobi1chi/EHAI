import type { InboxKind } from "../lib/api";
import { INBOX_KIND } from "../lib/labels";
import { DecisionCard } from "./DecisionCard";
import { Panel } from "./Panel";

export type InboxTarget = { readonly workspaceId: string; readonly kind: InboxKind; readonly requestId: string };
export { WorkflowDecisionForm, CaptureDecision } from "./DecisionCard";

export function InboxPanel({ target, onClose }: { target: InboxTarget; onClose: () => void }) {
  return <Panel title={INBOX_KIND[target.kind]} onClose={onClose}><DecisionCard target={target} /></Panel>;
}
