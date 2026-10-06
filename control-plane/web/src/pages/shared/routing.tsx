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
        <div className="meta pre">依据：{String(result["evidence"] ?? "")}</div>
        <div className="meta">外部 Agent 报告，EHAI 没有核实其中的外部事实。</div>
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
        <p className="meta">来自待办 · 只读查询，没有修改任何东西{result["truncated"] ? " · 结果过长已截断" : ""}</p>
      </div>
    );
  }
  if (isObject(data) && isObject(data["inbox"])) {
    const view = data["inbox"] as unknown as InboxListView;
    const items = view.items.filter((i) => i.pending);
    return (
      <div className="section" style={{ gap: 0 }}>
        {items.length === 0 && <p className="meta">没有等待处理的事项。</p>}
        {items.slice(0, 20).map((i) => (
          <div key={i.kind + i.request_id} className="row" style={{ minHeight: 36, padding: "4px 8px" }}>
            <span className="grow">{inboxQuestion(i)}</span>
            <span className="meta">{INBOX_KIND[i.kind]}</span>
          </div>
        ))}
        <p className="meta">来自需要我处理 · 只读查询</p>
      </div>
    );
  }
  return <div className="code">{JSON.stringify(result, null, 2)}</div>;
}

export function RouteLine({ request, lab }: { request: RoutingRequest; lab?: RoutingLabView }) {
  const j = request.judgement;
  switch (request.status) {
    case "queued":
    case "routing":
      return <span className="meta">等待快环判断…</span>;
    case "shadow":
      return (
        <span className="meta">
          影子模式：判断为「{recipeName(request, lab) || "慢环"}」，只记录不执行{j ? ` · 置信度 ${j.confidence.toFixed(2)}` : ""}
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
    const label = { correct: "已标记正确", misroute: "已标记走错", execution_failed: "已标记执行失败" }[request.feedback.outcome];
    return (
      <span className="meta">
        {label} · {request.feedback.actor}
      </span>
    );
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
      toast(kind === "correct" ? "已记为正确" : "已暂停这个配方，问题交回慢环");
    }
  }

  return (
    <div className="section" style={{ gap: 8 }}>
      {!open && (
        <div className="actions" style={{ justifyContent: "flex-start" }}>
          <button type="button" className="btn small" disabled={submission.pending} onClick={() => void send("correct", "结果正确")}>
            正确
          </button>
          <button type="button" className="btn small" onClick={() => setOpen(true)}>
            处理错了
          </button>
        </div>
      )}
      {open && (
        <div className="card" ref={form} style={{ scrollMarginBottom: 160 }}>
          <div className="segmented" role="tablist" aria-label="哪里错了">
            <button type="button" role="tab" aria-selected={outcome === "misroute"} onClick={() => setOutcome("misroute")}>
              理解错了
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={outcome === "execution_failed"}
              onClick={() => setOutcome("execution_failed")}
            >
              结果不对
            </button>
          </div>
          <label className="field">
            <span>说明</span>
            <textarea className="textarea" value={explanation} onChange={(e) => setExplanation(e.target.value)} />
          </label>
          <p className="meta">提交后会暂停这个配方，问题交回慢环；恢复配方前需要重新回放。</p>
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
              提交纠错
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

export function requestAge(request: RoutingRequest): string {
  return ago(request.created_at);
}

export { RECIPE_TARGET };
