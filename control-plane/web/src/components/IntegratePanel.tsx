import { useState } from "react";
import { core, type CodeIntegration } from "../lib/api";
import { useApp } from "../lib/app";
import { shortId } from "../lib/format";
import { useSubmission } from "../lib/submit";
import { Panel } from "./Panel";
import { ErrorBlock, Spinner } from "./Status";

/**
 * Materialise a Run's current valid code results into a separate integration workspace. The
 * core checks the process revision it was given, so a Run that changed since it was read is
 * refused rather than integrated from a stale view. Nothing is merged into the user's branch.
 */
export function IntegratePanel({
  workspaceId,
  runId,
  onClose,
}: {
  workspaceId: string;
  runId: string;
  onClose: () => void;
}) {
  const { touch } = useApp();
  const submission = useSubmission("integrate");
  const [result, setResult] = useState<CodeIntegration | null>(null);

  async function integrate() {
    const client = core(workspaceId);
    const outcome = await submission.run(async () => {
      const plan = await client.getRunPlan(runId);
      const processRevision = plan.data.process_revision_id;
      if (processRevision === null) throw new Error("这个 Run 还没有可整合的执行过程。");
      return client.integrateRun(runId, { expected_process_revision_id: processRevision });
    });
    if (outcome === undefined) return;
    setResult(outcome.data);
    touch(workspaceId);
  }

  return (
    <Panel
      title="取得代码成果"
      subtitle={`Run ${shortId(runId)}`}
      onClose={onClose}
      footer={
        <div className="actions">
          <button type="button" className="btn" onClick={onClose}>
            关闭
          </button>
          {result === null && (
            <button type="button" className="btn primary" disabled={submission.pending} onClick={() => void integrate()}>
              {submission.pending ? <Spinner /> : "整合成果"}
            </button>
          )}
        </div>
      }
    >
      {result === null ? (
        <p className="meta">
          把这个 Run 里所有通过检查的代码成果整合到一个独立的工作区并生成一个 commit。不会改动你当前的分支，也不会推送；
          看过差异后再按你的 Git 流程合进来。
        </p>
      ) : result.status === "integrated" ? (
        <dl className="kv">
          <dt>结果</dt>
          <dd className="ok">已整合</dd>
          <dt>commit</dt>
          <dd className="mono">{result.commit ?? "—"}</dd>
          <dt>基线</dt>
          <dd className="mono">{result.base_commit}</dd>
          <dt>工作区</dt>
          <dd className="mono">{result.workspace}</dd>
          <dt>差异文件</dt>
          <dd className="mono">{result.diff_path ?? "—"}</dd>
          <dt>来源</dt>
          <dd>{result.sources.length} 个任务的成果</dd>
        </dl>
      ) : (
        <>
          <div className="banner bad">
            <div className="banner-title">整合时有冲突</div>
            <div className="meta">冲突保留在下面的工作区里，处理后再按你的 Git 流程纳入。</div>
          </div>
          <dl className="kv">
            <dt>工作区</dt>
            <dd className="mono">{result.workspace}</dd>
            <dt>冲突文件</dt>
            <dd className="mono pre">{result.conflicts.join("\n")}</dd>
          </dl>
        </>
      )}
      {submission.error !== undefined && <ErrorBlock error={submission.error} />}
    </Panel>
  );
}
