import { core, manager, type PlanGraph } from "../lib/api";
import { useApp, useQuery } from "../lib/app";
import { useSubmission } from "../lib/submit";
import { Panel } from "./Panel";
import { ErrorBlock, QueryView, Spinner } from "./Status";

/**
 * Approve a draft plan and authorize its Run in one confirmation. The core still records two
 * facts: approval of the plan and its contract, then a Run started with the exact execution
 * config the workspace was registered with, which is shown here before it is authorized.
 */
export function StartRunPanel({
  workspaceId,
  plan,
  onClose,
  onStarted,
}: {
  workspaceId: string;
  plan: PlanGraph;
  onClose: () => void;
  onStarted: (runId: string) => void;
}) {
  const { touch } = useApp();
  const approval = useSubmission("approve");
  const start = useSubmission("start-run");
  const config = useQuery(`execution-config:${workspaceId}`, () =>
    manager.getWorkspaceExecutionConfig(workspaceId).then((r) => r.data.execution_config),
  );
  const pending = approval.pending || start.pending;

  async function confirm() {
    const executionConfig = config.data;
    if (executionConfig === undefined) return;
    const client = core(workspaceId);
    if (plan.status === "draft") {
      const approved = await approval.run((key) =>
        client.approvePlan({
          idempotency_key: key,
          plan_revision_id: plan.plan_revision_id,
          completion_contract_id: plan.completion_contract_id,
        }),
      );
      if (approved === undefined) return;
      touch(workspaceId);
    }
    const run = await start.run((key) =>
      // Without a Pi config the host runs its own configured Worker; the core decides.
      executionConfig === null
        ? client.startRun({ idempotency_key: key, plan_revision_id: plan.plan_revision_id })
        : client.startRun({
            idempotency_key: key,
            plan_revision_id: plan.plan_revision_id,
            execution_config: executionConfig,
          }),
    );
    if (run === undefined) return;
    touch(workspaceId);
    onStarted(run.data.run_id);
  }

  return (
    <Panel
      title={plan.status === "draft" ? "批准方案并开始执行" : "开始执行"}
      subtitle={`方案第 ${plan.version} 版 · ${plan.nodes.length} 个任务`}
      onClose={onClose}
      footer={
        <div className="actions">
          <button type="button" className="btn" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={pending || config.data === undefined}
            onClick={() => void confirm()}
          >
            {pending ? <Spinner /> : plan.status === "draft" ? "批准并开始" : "开始执行"}
          </button>
        </div>
      }
    >
      <p className="meta">
        批准会固定这一版的需求、任务图和验收条件；开始执行会用下面这份配置授权 Worker 在这个工作区里工作。
      </p>
      <QueryView query={config}>
        {(executionConfig) =>
          executionConfig === null ? (
            <div className="banner">
              <div className="banner-title">这个工作区不使用模型 Worker</div>
              <div className="meta">它登记的不是 Pi Worker，执行会使用宿主自己配置的 Worker，不调用模型。</div>
            </div>
          ) : (
            <dl className="kv">
              <dt>工作区</dt>
              <dd className="mono">{executionConfig.workspace}</dd>
              <dt>模型</dt>
              <dd className="mono">
                {executionConfig.model}
                {executionConfig.reasoning_effort ? ` · ${executionConfig.reasoning_effort}` : ""}
              </dd>
              <dt>并发</dt>
              <dd>{executionConfig.capacity}</dd>
              <dt>允许的命令</dt>
              <dd className="mono pre">
                {executionConfig.allowed_commands.length
                  ? executionConfig.allowed_commands.map((argv) => argv.join(" ")).join("\n")
                  : "无"}
              </dd>
              <dt>Shell</dt>
              <dd className="mono">{executionConfig.available_shells.join("、") || "无"}</dd>
              <dt>Git 权限</dt>
              <dd className="mono">{executionConfig.git_permissions.join("、") || "无"}</dd>
              <dt>命令超时</dt>
              <dd>{executionConfig.command_timeout_seconds} 秒</dd>
            </dl>
          )
        }
      </QueryView>
      {approval.error !== undefined && <ErrorBlock error={approval.error} />}
      {start.error !== undefined && <ErrorBlock error={start.error} />}
    </Panel>
  );
}
