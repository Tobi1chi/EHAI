import { useState } from "react";
import { core, type CodeIntegration, type IntegrationBranch } from "../lib/api";
import { useApp, useQuery } from "../lib/app";
import { shortId } from "../lib/format";
import { useSubmission } from "../lib/submit";
import { DiffView } from "./DiffView";
import { Panel } from "./Panel";
import { ErrorBlock, QueryView, Spinner } from "./Status";

/**
 * Materialise a Run's current valid code results into a separate integration workspace, or
 * reopen one the core already recorded. The core checks the process revision it was given, so
 * a Run that changed since it was read is refused rather than integrated from a stale view.
 * Nothing is merged into the user's branch.
 */
export function IntegratePanel({
  workspaceId,
  runId,
  integrationId,
  onClose,
}: {
  workspaceId: string;
  runId: string;
  /** Recorded integration to show; without it the latest recorded one is shown. */
  integrationId?: string;
  onClose: () => void;
}) {
  const { touch } = useApp();
  const submission = useSubmission("integrate");
  const [fresh, setFresh] = useState<CodeIntegration | null>(null);
  const recorded = useQuery(`integrations:${workspaceId}:${runId}`, () =>
    core(workspaceId)
      .listRunIntegrations(runId)
      .then((r) => r.data),
  );

  async function integrate() {
    const client = core(workspaceId);
    const outcome = await submission.run(async () => {
      const plan = await client.getRunPlan(runId);
      const processRevision = plan.data.process_revision_id;
      if (processRevision === null) throw new Error("这个 Run 还没有可整合的执行过程。");
      return client.integrateRun(runId, { expected_process_revision_id: processRevision });
    });
    if (outcome === undefined) return;
    setFresh(outcome.data);
    recorded.reload();
    touch(workspaceId);
  }

  function shown(list: ReadonlyArray<CodeIntegration>): CodeIntegration | null {
    if (fresh !== null) return fresh;
    if (integrationId !== undefined) return list.find((item) => item.integration_id === integrationId) ?? null;
    return list.at(-1) ?? null;
  }

  const current = recorded.data === undefined ? null : shown(recorded.data);

  return (
    <Panel
      wide={current?.status === "integrated"}
      title="代码成果"
      subtitle={`Run ${shortId(runId)}`}
      onClose={onClose}
      footer={
        <div className="actions">
          <button type="button" className="btn" onClick={onClose}>
            关闭
          </button>
          {recorded.data !== undefined && (
            <button
              type="button"
              className={current === null ? "btn primary" : "btn"}
              disabled={submission.pending}
              onClick={() => void integrate()}
            >
              {submission.pending ? <Spinner /> : current === null ? "整合成果" : "重新整合"}
            </button>
          )}
        </div>
      }
    >
      <QueryView query={recorded}>
        {() =>
          current === null ? (
            <p className="meta">
              把这个 Run 里所有通过检查的代码成果整合到一个独立的工作区并生成一个 commit。不会改动你当前的分支，也不会推送；
              看过差异后再按你的 Git 流程合进来。
            </p>
          ) : current.status === "integrated" ? (
            <>
              <dl className="kv">
                <dt>结果</dt>
                <dd className="ok">已整合 · {current.sources.length} 个任务的成果</dd>
                <dt>commit</dt>
                <dd className="mono">{current.commit ?? "—"}</dd>
                <dt>基线</dt>
                <dd className="mono">{current.base_commit}</dd>
                <dt>工作区</dt>
                <dd className="mono">{current.workspace}</dd>
              </dl>
              <BranchForm workspaceId={workspaceId} runId={runId} integration={current} />
              <DiffView workspaceId={workspaceId} runId={runId} integrationId={current.integration_id} />
            </>
          ) : (
            <>
              <div className="banner bad">
                <div className="banner-title">整合时有冲突</div>
                <div className="meta">
                  冲突保留在下面的工作区里。在那里解决并暂存冲突后点“重新整合”，EHAI 会继续这次合并。
                </div>
              </div>
              <dl className="kv">
                <dt>工作区</dt>
                <dd className="mono">{current.workspace}</dd>
                <dt>冲突文件</dt>
                <dd className="mono pre">{current.conflicts.join("\n")}</dd>
              </dl>
            </>
          )
        }
      </QueryView>
      {submission.error !== undefined && <ErrorBlock error={submission.error} />}
    </Panel>
  );
}

/**
 * Point a new branch in the user's repository at the integrated commit. The core never checks it
 * out or pushes it; the user merges or opens a PR with their own Git flow.
 */
function BranchForm({
  workspaceId,
  runId,
  integration,
}: {
  workspaceId: string;
  runId: string;
  integration: CodeIntegration;
}) {
  const { touch } = useApp();
  const submission = useSubmission("integration-branch");
  const [branch, setBranch] = useState(`ehai/run-${shortId(runId)}`);
  const [created, setCreated] = useState<IntegrationBranch | null>(null);

  async function create() {
    const outcome = await submission.run(() =>
      core(workspaceId).createIntegrationBranch(runId, integration.integration_id, { branch: branch.trim() }),
    );
    if (outcome === undefined) return;
    setCreated(outcome.data);
    touch(workspaceId);
  }

  if (created !== null) {
    return (
      <div className="banner">
        <div className="banner-title ok">
          {created.created ? `已创建分支 ${created.branch}` : `分支 ${created.branch} 已经指向这个结果`}
        </div>
        <div className="meta">没有切换你当前的分支，也没有推送。用你的 Git 流程合并或开 PR：</div>
        <div className="code">{`git switch ${created.branch}`}</div>
      </div>
    );
  }

  return (
    <div className="field">
      <span>放到新分支</span>
      <div className="actions" style={{ flexWrap: "nowrap" }}>
        <input className="input mono" value={branch} onChange={(e) => setBranch(e.target.value)} />
        <button
          type="button"
          className="btn primary"
          disabled={submission.pending || !branch.trim()}
          onClick={() => void create()}
        >
          {submission.pending ? <Spinner /> : "创建分支"}
        </button>
      </div>
      <span className="meta">在你的仓库里新建这个分支指向整合 commit；不切换、不推送，已存在的同名分支不会被覆盖。</span>
      {submission.error !== undefined && <ErrorBlock error={submission.error} />}
    </div>
  );
}
