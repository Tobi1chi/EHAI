import { useState } from "react";
import { Icon } from "../../components/Icon";
import { ErrorBlock } from "../../components/Status";
import { core, type WorkflowRun } from "../../lib/api";
import { useApp } from "../../lib/app";
import { Link, navigate } from "../../lib/router";
import { useSubmission } from "../../lib/submit";
import { LifeGate, type Life } from "./LifeGate";
import { joinDue, takeDraft } from "./data";

type Draft = { readonly id: number; title: string; date: string; time: string };

let nextId = 1;
function blank(title = ""): Draft {
  return { id: nextId++, title, date: "", time: "" };
}

/** Split the source text by lines or Chinese/English semicolons; purely a typing aid. */
function splitSource(text: string): string[] {
  return text
    .split(/[\n;；]+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, 100);
}

function Capture({ life }: { life: Life }) {
  const { touch } = useApp();
  const [source, setSource] = useState(() => takeDraft());
  const [rows, setRows] = useState<Draft[]>(() => [blank()]);
  const [confirm, setConfirm] = useState(true);
  const [done, setDone] = useState<WorkflowRun | null>(null);
  const submission = useSubmission("capture");
  const valid = rows.filter((r) => r.title.trim());

  function update(id: number, change: Partial<Draft>) {
    setRows((current) => current.map((r) => (r.id === id ? { ...r, ...change } : r)));
  }

  async function submit() {
    const result = await submission.run((key) =>
      core(life.workspaceId).startWorkflow(life.projectId, {
        idempotency_key: key,
        workflow: "life.capture",
        // The core keeps the original wording; without one, the typed titles are that wording.
        source_text: source.trim() || valid.map((r) => r.title.trim()).join("\n"),
        tasks: valid.map((r) => ({ title: r.title.trim(), due_at: joinDue(r.date, r.time) })),
        require_confirmation: confirm,
      }),
    );
    if (result) {
      touch(life.workspaceId);
      setDone(result.data);
    }
  }

  if (done) {
    const waiting = done.status === "awaiting_confirmation";
    return (
      <div className="content">
        <h1 className="page-title">{waiting ? "已提交，等待确认" : `已保存 ${done.task_ids.length} 项待办`}</h1>
        <p className="body muted">
          {waiting
            ? "这次录入出现在「需要我处理」里。确认之前不会保存任何待办。"
            : "录入时选择了不需要确认，待办已经直接保存。"}
        </p>
        <div className="actions" style={{ justifyContent: "flex-start" }}>
          <Link className="btn" to="/life">
            回到今天
          </Link>
          {waiting ? (
            <Link className="btn primary" to={`/life/records/${done.workflow_run_id}`}>
              现在确认
            </Link>
          ) : (
            <Link className="btn primary" to="/life/tasks">
              查看待办
            </Link>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="content">
      <div className="page-head">
        <div className="grow">
          <div className="meta">{life.projectName}</div>
          <h1 className="page-title">录入</h1>
        </div>
      </div>
      <label className="field">
        <span>原文</span>
        <textarea
          className="textarea"
          value={source}
          onChange={(e) => setSource(e.target.value)}
          placeholder="比如：买牛奶；预约周末保洁"
        />
        <span className="meta">原样保存，方便以后对照。不填时用事项标题作为原文。</span>
      </label>

      <section className="section">
        <div className="section-head">
          <h2 className="h2 grow">事项 · {valid.length}</h2>
          <button
            type="button"
            className="btn small ghost"
            disabled={!source.trim()}
            onClick={() => setRows(splitSource(source).map((t) => blank(t)))}
          >
            按行拆成事项
          </button>
        </div>
        <p className="meta">EHAI 不会自动拆分原文。可以手动填写，也可以让外部 Agent 整理后提交。</p>
        {rows.map((row, index) => (
          <div key={row.id} className="card" style={{ gap: 8 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span className="meta">事项 {index + 1}</span>
              <span style={{ flex: "1 1 auto" }} />
              {rows.length > 1 && (
                <button
                  type="button"
                  className="icon-btn"
                  aria-label={`删除事项 ${index + 1}`}
                  onClick={() => setRows((current) => current.filter((r) => r.id !== row.id))}
                >
                  <Icon name="close" size={16} />
                </button>
              )}
            </div>
            <input
              className="input"
              aria-label={`事项 ${index + 1} 标题`}
              placeholder="标题"
              value={row.title}
              onChange={(e) => update(row.id, { title: e.target.value })}
            />
            <div className="split" style={{ gap: 8 }}>
              <input
                className="input"
                type="date"
                aria-label={`事项 ${index + 1} 截止日期`}
                value={row.date}
                onChange={(e) => update(row.id, { date: e.target.value })}
              />
              <input
                className="input"
                type="time"
                aria-label={`事项 ${index + 1} 截止时间`}
                value={row.time}
                disabled={!row.date}
                onChange={(e) => update(row.id, { time: e.target.value })}
              />
            </div>
          </div>
        ))}
        <div>
          <button type="button" className="btn small" onClick={() => setRows((current) => [...current, blank()])}>
            <Icon name="plus" size={14} strokeWidth={2.2} />
            添加事项
          </button>
        </div>
      </section>

      <label className="row" style={{ cursor: "pointer" }}>
        <span className="grow">
          <span className="title">先确认再保存</span>
          <span className="meta">
            {confirm ? "提交后出现在「需要我处理」，批准后才保存。" : "提交后直接保存为待办。"}
          </span>
        </span>
        <button
          type="button"
          className="switch"
          role="switch"
          aria-checked={confirm}
          aria-label="先确认再保存"
          onClick={() => setConfirm((v) => !v)}
        >
          <span />
        </button>
      </label>

      {submission.error !== undefined && <ErrorBlock error={submission.error} />}
      <div className="actions">
        <button type="button" className="btn" onClick={() => navigate("/life")}>
          取消
        </button>
        <button
          type="button"
          className="btn primary large"
          disabled={submission.pending || valid.length === 0}
          onClick={() => void submit()}
        >
          {confirm ? `提交 ${valid.length} 项待确认` : `保存 ${valid.length} 项`}
        </button>
      </div>
    </div>
  );
}

export function LifeCapture() {
  return <LifeGate>{(life) => <Capture life={life} />}</LifeGate>;
}
