import { useState } from "react";
import { ErrorBlock, QueryView } from "../../components/Status";
import { useToast } from "../../components/Toast";
import { core, type ConnectorCall, type ConnectorConnection } from "../../lib/api";
import { useApp, useQuery } from "../../lib/app";
import { ago, json, shortId } from "../../lib/format";
import { isLife, projectRows } from "../../lib/overview";
import { usePrefs } from "../../lib/prefs";
import { useSubmission } from "../../lib/submit";

type Row = {
  readonly workspaceId: string;
  readonly projectId: string;
  readonly projectName: string;
  readonly connector: ConnectorConnection;
};

const CALL_STATUS: Record<ConnectorCall["status"], string> = {
  queued: "排队中",
  claimed: "执行中",
  completed: "完成",
  failed: "失败",
  unknown: "结果未知",
};

function useAllConnectors() {
  const { overview } = useApp();
  const projects = projectRows(overview.data);
  const key = projects.map((p) => p.workspaceId + ":" + p.detail.project.project_id).join(",");
  return useQuery(
    overview.data ? `all-connectors:${key}` : null,
    () =>
      Promise.all(
        projects.map((p) =>
          core(p.workspaceId)
            .listConnectors(p.detail.project.project_id)
            .then((r) =>
              r.data.map(
                (connector): Row => ({
                  workspaceId: p.workspaceId,
                  projectId: p.detail.project.project_id,
                  projectName: p.detail.project.name,
                  connector,
                }),
              ),
            ),
        ),
      ).then((lists) => lists.flat()),
    { workspaces: [...new Set(projects.map((p) => p.workspaceId))] },
  );
}

export function ConnectionsPage() {
  const prefs = usePrefs();
  const connectors = useAllConnectors();
  const [selected, setSelected] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const rows = connectors.data ?? [];
  const current = rows.find((r) => r.connector.connector_id === selected) ?? null;
  const groups = [
    { title: "生活", rows: rows.filter((r) => isLife(prefs.life, r.workspaceId, r.projectId)) },
    { title: "工作", rows: rows.filter((r) => !isLife(prefs.life, r.workspaceId, r.projectId)) },
  ];

  return (
    <div className="content wide">
      <div className="page-head">
        <div className="grow">
          <div className="meta">账号授权在你电脑的命令行里完成，不经过网页</div>
          <h1 className="page-title">连接</h1>
        </div>
        <button type="button" className="btn" onClick={() => setAdding((v) => !v)}>
          新建连接
        </button>
      </div>
      {adding && <AddConnector />}
      <QueryView query={connectors}>
        {() =>
          rows.length === 0 ? (
            <p className="empty">还没有连接。</p>
          ) : (
            <div className="split">
              <div className="section">
                {groups.map(
                  (g) =>
                    g.rows.length > 0 && (
                      <section key={g.title} className="section">
                        <h2 className="meta" style={{ fontWeight: 600 }}>
                          {g.title}
                        </h2>
                        <div className="list">
                          {g.rows.map((r) => (
                            <button
                              key={r.connector.connector_id}
                              type="button"
                              className="row"
                              aria-current={selected === r.connector.connector_id ? "true" : undefined}
                              style={selected === r.connector.connector_id ? { background: "var(--active)" } : undefined}
                              onClick={() => setSelected(r.connector.connector_id)}
                            >
                              <span className="grow">
                                <span className="title">{r.connector.name}</span>
                                <span className="meta">
                                  {r.connector.manifest.connector_type} · {r.workspaceId} / {r.projectName}
                                </span>
                              </span>
                            </button>
                          ))}
                        </div>
                      </section>
                    ),
                )}
              </div>
              <div>{current ? <ConnectorDetail row={current} /> : <p className="empty">选择一个连接查看动作和调用记录。</p>}</div>
            </div>
          )
        }
      </QueryView>
    </div>
  );
}

function ConnectorDetail({ row }: { row: Row }) {
  const c = row.connector;
  const calls = useQuery(
    `connector-calls:${row.workspaceId}:${c.connector_id}`,
    () => core(row.workspaceId).listConnectorCalls(c.connector_id).then((r) => r.data),
    { workspaces: [row.workspaceId] },
  );
  const config = Object.keys(c.configuration).length ? json(c.configuration) : null;
  return (
    <div className="section" style={{ gap: 16 }}>
      <div>
        <div className="h2">{c.name}</div>
        <div className="meta">
          {c.manifest.connector_type} v{c.manifest.version} · <span className="mono">{c.connector_id}</span>
        </div>
        <div className="meta">
          {row.workspaceId} / {row.projectName} · 登记于 {ago(c.created_at)}
        </div>
      </div>
      <p className="meta">连接登记后不能改。要换账号，新建一个连接。</p>
      {config && <div className="code">{config}</div>}
      <section className="section">
        <h3 className="h2">动作</h3>
        <div className="list">
          {c.manifest.actions.map((a) => (
            <div key={a.name + a.version} className="row" style={{ minHeight: 44 }}>
              <span className="grow">
                <span className="mono">
                  {a.name} v{a.version}
                </span>
                <span className="meta">{a.description}</span>
              </span>
              <span className={`tag ${a.read_only ? "" : "attn"}`}>{a.read_only ? "只读" : "会改动外部数据"}</span>
            </div>
          ))}
        </div>
      </section>
      {(c.manifest.events ?? []).length > 0 && (
        <section className="section">
          <h3 className="h2">会推送的事件</h3>
          <div className="tags">
            {(c.manifest.events ?? []).map((e) => (
              <span key={e.name + e.version} className="tag mono">
                {e.name} v{e.version}
              </span>
            ))}
          </div>
        </section>
      )}
      <section className="section">
        <h3 className="h2">调用记录</h3>
        <QueryView query={calls}>
          {(data) =>
            data.length === 0 ? (
              <p className="empty">还没有调用。</p>
            ) : (
              <div className="list">
                {[...data].reverse().slice(0, 50).map((call) => (
                  <CallRow key={call.call_id} workspaceId={row.workspaceId} call={call} />
                ))}
              </div>
            )
          }
        </QueryView>
      </section>
    </div>
  );
}

function CallRow({ workspaceId, call }: { workspaceId: string; call: ConnectorCall }) {
  const { touch } = useApp();
  const toast = useToast();
  const submission = useSubmission("reconcile");
  const tone = call.status === "completed" ? "ok" : call.status === "failed" ? "bad" : call.status === "unknown" ? "attn" : "";

  async function reconcile() {
    const result = await submission.run((key) =>
      core(workspaceId).reconcileConnectorCall(call.call_id, { idempotency_key: key }),
    );
    if (result) {
      touch(workspaceId);
      toast("正在查结果");
    }
  }

  return (
    <div className="row" style={{ alignItems: "flex-start" }}>
      <span className="grow">
        <span className="mono">
          {call.action.name} v{call.action.version}
        </span>
        <span className="meta">
          {shortId(call.call_id)} · {ago(call.created_at)}
          {call.write_authorized ? " · 允许改动" : ""}
          {call.error ? ` · ${call.error}` : ""}
        </span>
        {call.status === "unknown" && (
          <>
            <span className="meta attn">
              请求发出去了，但没收到回复，对方可能已经执行过。为了避免重复，不会自动重试。
            </span>
            {submission.error !== undefined && <ErrorBlock error={submission.error} />}
            <span>
              <button type="button" className="btn small" disabled={submission.pending} onClick={() => void reconcile()}>
                查一下结果
              </button>
            </span>
          </>
        )}
      </span>
      <span className={`tag ${tone}`}>{CALL_STATUS[call.status]}</span>
    </div>
  );
}

function AddConnector() {
  const { overview } = useApp();
  const projects = projectRows(overview.data);
  const [choice, setChoice] = useState(0);
  const project = projects[choice];
  const api = project ? `${window.location.origin}/workspaces/${project.workspaceId}` : "<工作区地址>";
  const projectId = project?.detail.project.project_id ?? "<项目 ID>";
  return (
    <div className="card">
      <div className="h2">新建连接</div>
      <p className="meta">在终端里依次运行下面的命令。账号授权在命令行里完成，不经过网页。</p>
      {projects.length > 0 && (
        <label className="field">
          <span>登记到哪个项目</span>
          <select className="select" value={choice} onChange={(e) => setChoice(Number(e.target.value))}>
            {projects.map((p, i) => (
              <option key={p.workspaceId + p.detail.project.project_id} value={i}>
                {p.workspaceId} / {p.detail.project.name}
              </option>
            ))}
          </select>
        </label>
      )}
      <div className="section">
        <div className="meta">Google 日历</div>
        <div className="code">
          {`uv run ehai-google-calendar init --api-url ${api} --project-id ${projectId} --state-dir <私有目录> --calendar-id primary\nuv run ehai-google-calendar authorize --state-dir <私有目录> --client-config <Google 桌面客户端 JSON>\nuv run ehai-google-calendar run --state-dir <私有目录>`}
        </div>
        <div className="meta">Jev（快环）</div>
        <div className="code">
          {`uv run ehai-jev init --api-url ${api} --project-id ${projectId} --state-dir <私有目录> --model jev-latest\nuv run ehai-jev run --state-dir <私有目录> --key-file <私有密钥文件>`}
        </div>
        <p className="meta">私有目录不要放在代码仓库里。登记好之后，连接会显示在下面。</p>
      </div>
    </div>
  );
}
