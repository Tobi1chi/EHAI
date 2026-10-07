import { useEffect, useRef, useState } from "react";
import { ErrorBlock } from "../../components/Status";
import { useToast } from "../../components/Toast";
import {
  core,
  type InboxListView,
  type JsonValue,
  type LifeTask,
  type RoutingLabView,
  type RoutingRequest,
} from "../../lib/api";
import { useApp, useQuery } from "../../lib/app";
import { ago, when } from "../../lib/format";
import { INBOX_KIND, inboxQuestion, RECIPE_TARGET, routingReason } from "../../lib/labels";
import { actorName, setPrefs, usePrefs } from "../../lib/prefs";
import { useSubmission } from "../../lib/submit";
import { sortByDue } from "../life/data";

export function useLabs(workspaceId: string | null, projectId: string | null) {
  return useQuery(
    workspaceId && projectId ? `labs:${workspaceId}:${projectId}` : null,
    () => core(workspaceId as string).listRoutingLabs(projectId as string).then((r) => r.data),
    { workspaces: workspaceId ? [workspaceId] : [] },
  );
}

/** Pick one lab per Project; the choice is remembered in this browser. */
export function useChosenLab(
  scope: string,
  labs: ReadonlyArray<RoutingLabView> | undefined,
): [RoutingLabView | undefined, (labId: string) => void] {
  const prefs = usePrefs();
  const wanted = prefs.labs[scope];
  const chosen = labs?.find((l) => l.lab.lab_id === wanted) ?? labs?.[0];
  return [chosen, (labId) => setPrefs({ labs: { ...prefs.labs, [scope]: labId } })];
}

export function LabPicker({
  labs,
  chosen,
  onChoose,
}: {
  labs: ReadonlyArray<RoutingLabView>;
  chosen: RoutingLabView;
  onChoose: (labId: string) => void;
}) {
  if (labs.length < 2) return null;
  return (
    <label className="field">
      <span>实验</span>
      <select className="select" value={chosen.lab.lab_id} onChange={(e) => onChoose(e.target.value)}>
        {labs.map((l) => (
          <option key={l.lab.lab_id} value={l.lab.lab_id}>
            {l.lab.name}
          </option>
        ))}
      </select>
    </label>
  );
}

export function recipeName(request: RoutingRequest, labs?: RoutingLabView): string {
  const id = request.selected_recipe_id;
  if (!id) return "";
  const recipe =
    request.catalog_snapshot.find((r) => r.recipe_id === id) ?? labs?.recipes.find((r) => r.recipe_id === id);
  return recipe ? recipe.name : id.slice(0, 8);
}

function isObject(value: JsonValue | undefined): value is { readonly [key: string]: JsonValue } {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Render the read-only query a fast-path recipe returned, or the System 2 report. */
export function RoutingResult({ request }: { request: RoutingRequest }) {
  const result = request.result;
  if (!result) return null;
  if (result["evidence_kind"] === "external_agent_report") {
    return (
      <div className="section" style={{ gap: 4 }}>
        <div className="body pre">{String(result["response"] ?? "")}</div>
        <div className="meta pre">依据：{String(result["evidence"] ?? "")} · 未经核实</div>
      </div>
    );
  }
  const data = result["data"];
  if (isObject(data) && Array.isArray(data["items"])) {
    const tasks = sortByDue((data["items"] as unknown as LifeTask[]).filter((t) => t.status === "open"));
    return (
      <div className="section" style={{ gap: 0 }}>
        {tasks.length === 0 && <p className="meta">没有未完成的待办。</p>}
        {tasks.slice(0, 20).map((t) => (
          <div key={t.task_id} className="row" style={{ minHeight: 36, padding: "4px 8px" }}>
            <span className="grow">{t.title}</span>
            {t.due_at && <span className="meta">{when(t.due_at)}</span>}
          </div>
        ))}
        <p className="meta">来自待办{result["truncated"] ? " · 太多了，只列出一部分" : ""}</p>
      </div>
    );
  }
  if (isObject(data) && isObject(data["inbox"])) {
    const view = data["inbox"] as unknown as InboxListView;
    const items = view.items.filter((i) => i.pending);
    return (
      <div className="section" style={{ gap: 0 }}>
        {items.length === 0 && <p className="meta">没有要你处理的事。</p>}
        {items.slice(0, 20).map((i) => (
          <div key={i.kind + i.request_id} className="row" style={{ minHeight: 36, padding: "4px 8px" }}>
            <span className="grow">{inboxQuestion(i)}</span>
            <span className="meta">{INBOX_KIND[i.kind]}</span>
          </div>
        ))}
        <p className="meta">来自待处理</p>
      </div>
    );
  }
  return <div className="code">{JSON.stringify(result, null, 2)}</div>;
}

/** Plain wording for the 问 page; the routing details stay on 学习与发布. */
function PlainRouteLine({ request }: { request: RoutingRequest }) {
  switch (request.status) {
    case "queued":
    case "routing":
      return <span className="meta">正在处理…</span>;
    case "shadow":
      return <span className="meta">试运行中，只记录不回答</span>;
    case "completed":
      return null;
    case "escalated":
      return (
        <span className="meta attn">
          {request.fallback?.status === "failed"
            ? "处理失败了"
            : request.feedback
              ? "已转交重新处理，处理好会显示在这里"
              : "这个问题要多花点时间，处理好会显示在这里"}
        </span>
      );
  }
}

export function RouteLine({ request, lab, plain = false }: { request: RoutingRequest; lab?: RoutingLabView; plain?: boolean }) {
  if (plain) return <PlainRouteLine request={request} />;
  const j = request.judgement;
  switch (request.status) {
    case "queued":
    case "routing":
      return <span className="meta">等待快环判断…</span>;
    case "shadow":
      return (
        <span className="meta">
          试运行：判断为「{recipeName(request, lab) || "慢环"}」，只记录不回答{j ? ` · 置信度 ${j.confidence.toFixed(2)}` : ""}
        </span>
      );
    case "completed":
      if (request.route_source === "system2") return <span className="meta">慢环 · 外部 Agent 处理</span>;
      return (
        <span className="meta">
          快环 · 配方「{recipeName(request, lab)}」
          {request.route_source === "rule" ? " · 指定绑定" : ""}
          {j ? ` · 置信度 ${j.confidence.toFixed(2)}` : ""}
        </span>
      );
    case "escalated": {
      const fallback = request.fallback;
      return (
        <span className="meta attn">
          已交给慢环 · {routingReason(request.reason)}
          {fallback ? ` · Pi ${fallback.status === "running" ? "正在处理" : fallback.status === "completed" ? "已处理" : "失败"}` : ""}
        </span>
      );
    }
  }
}

export function FeedbackBar({ workspaceId, request }: { workspaceId: string; request: RoutingRequest }) {
  const prefs = usePrefs();
  const { touch } = useApp();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [outcome, setOutcome] = useState<"misroute" | "execution_failed">("misroute");
  const [explanation, setExplanation] = useState("");
  const submission = useSubmission("routing-feedback");
  const form = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // The form opens under the sticky composer; bring it into view.
    if (open) form.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [open]);

  if (request.feedback) {
    return <span className="meta">{FEEDBACK_LABEL[request.feedback.outcome]}</span>;
  }
  if (request.status !== "completed") return null;

  async function send(kind: "correct" | "misroute" | "execution_failed", text: string) {
    const result = await submission.run((key) =>
      core(workspaceId).recordRoutingFeedback(request.request_id, {
        idempotency_key: key,
        actor: actorName(prefs),
        outcome: kind,
        explanation: text,
      }),
    );
    if (result) {
      touch(workspaceId);
      setOpen(false);
      toast(kind === "correct" ? "谢谢反馈" : "收到，这类问题先不自动回答了");
    }
  }

  return (
    <div className="section" style={{ gap: 8 }}>
      {!open && (
        <div className="actions" style={{ justifyContent: "flex-start" }}>
          <button type="button" className="btn small" disabled={submission.pending} onClick={() => void send("correct", "结果正确")}>
            答对了
          </button>
          <button type="button" className="btn small" onClick={() => setOpen(true)}>
            答错了
          </button>
        </div>
      )}
      {open && (
        <div className="card" ref={form} style={{ scrollMarginBottom: 160 }}>
          <div className="segmented" role="tablist" aria-label="哪里错了">
            <button type="button" role="tab" aria-selected={outcome === "misroute"} onClick={() => setOutcome("misroute")}>
              没听懂问题
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={outcome === "execution_failed"}
              onClick={() => setOutcome("execution_failed")}
            >
              答案不对
            </button>
          </div>
          <label className="field">
            <span>哪里不对</span>
            <textarea className="textarea" value={explanation} onChange={(e) => setExplanation(e.target.value)} />
          </label>
          <p className="meta">提交后，这类问题先不自动回答，改为转交处理。之后可以在「学习与发布」里重新开启。</p>
          <div className="actions">
            <button type="button" className="btn" onClick={() => setOpen(false)}>
              取消
            </button>
            <button
              type="button"
              className="btn primary"
              disabled={submission.pending || !explanation.trim()}
              onClick={() => void send(outcome, explanation.trim())}
            >
              提交
            </button>
          </div>
        </div>
      )}
      {submission.error !== undefined && <ErrorBlock error={submission.error} />}
    </div>
  );
}

/**
 * The lab only moves when advance is called (external mode). While this page shows a
 * request that is still waiting, advance it on an interval, as the user would by hand.
 */
export function useAutoAdvance(workspaceId: string, labId: string | undefined, waiting: boolean): void {
  const { touch } = useApp();
  const ticks = useRef(0);
  useEffect(() => {
    if (!labId || !waiting) {
      ticks.current = 0;
      return;
    }
    const timer = window.setInterval(() => {
      if (document.hidden || ticks.current >= 60) return;
      ticks.current += 1;
      void core(workspaceId)
        .advanceRoutingLab(labId)
        .then(() => touch(workspaceId))
        .catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [workspaceId, labId, waiting, touch]);
}

export const FEEDBACK_LABEL: Record<"correct" | "misroute" | "execution_failed", string> = {
  correct: "你说答对了",
  misroute: "你说没听懂问题",
  execution_failed: "你说答案不对",
};

export function requestAge(request: RoutingRequest): string {
  return ago(request.created_at);
}

export { RECIPE_TARGET };
