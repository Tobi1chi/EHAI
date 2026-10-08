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

/** Rows for the given text, or one empty row. */
function rowsFrom(text: string): Draft[] {
  const titles = splitSource(text);
  return titles.length ? titles.map((t) => blank(t)) : [blank()];
}

function Capture({ life }: { life: Life }) {
  const { touch } = useApp();
  // Text typed on 今天 arrives as the original wording and is already split into items.
  const [initial] = useState(() => takeDraft());
  const [source, setSource] = useState(initial);
  const [rows, setRows] = useState<Draft[]>(() => rowsFrom(initial));
  // Typing items yourself needs no second confirmation; it is there for proposals from others.
  const [confirm, setConfirm] = useState(false);
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
        <h1 className="page-title">{waiting ? "等你确认" : `已保存 ${done.task_ids.length} 项待办`}</h1>
        {waiting && <p className="body muted">放在「待处理」里了，确认后才会保存。</p>}
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
        <span>原话（可选）</span>
        <textarea
          className="textarea"
          value={source}
          onChange={(e) => setSource(e.target.value)}
          placeholder="比如：买牛奶；预约周末保洁"
        />
        <span className="meta">一次记好几件事时，粘贴在这里，点「按行拆开」。原话会跟着记录保存。</span>
      </label>

      <section className="section">
        <div className="section-head">
          <h2 className="h2 grow">待办 · {valid.length}</h2>
          <button
            type="button"
            className="btn small ghost"
            disabled={!source.trim()}
            // Keep what was already typed; add only lines that are not there yet.
            onClick={() =>
              setRows((current) => {
                const kept = current.filter((r) => r.title.trim());
                const have = new Set(kept.map((r) => r.title.trim()));
                const added = splitSource(source).filter((t) => !have.has(t)).map((t) => blank(t));
                return kept.length + added.length ? [...kept, ...added] : [blank()];
              })
            }
          >
            按行拆开
          </button>
        </div>
        {rows.map((row, index) => (
          <div key={row.id} className="card" style={{ gap: 8 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span className="meta">第 {index + 1} 项</span>
              <span style={{ flex: "1 1 auto" }} />
              {rows.length > 1 && (
                <button
                  type="button"
                  className="icon-btn"
                  aria-label={`删除第 ${index + 1} 项`}
                  onClick={() => setRows((current) => current.filter((r) => r.id !== row.id))}
                >
                  <Icon name="close" size={16} />
                </button>
              )}
            </div>
            <input
              className="input"
              aria-label={`第 ${index + 1} 项要做什么`}
              placeholder="要做什么"
              value={row.title}
              onChange={(e) => update(row.id, { title: e.target.value })}
            />
            <div className="split" style={{ gap: 8 }}>
              <input
                className="input"
                type="date"
                aria-label={`第 ${index + 1} 项截止日期`}
                value={row.date}
                onChange={(e) => update(row.id, { date: e.target.value })}
              />
              <input
                className="input"
                type="time"
                aria-label={`第 ${index + 1} 项截止时间`}
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
            再加一项
          </button>
        </div>
      </section>

      <label className="row" style={{ cursor: "pointer" }}>
        <span className="grow">
          <span className="title">先放进待处理，确认后再保存</span>
          <span className="meta">别人帮你整理的待办可以先过一遍再保存。</span>
        </span>
        <button
          type="button"
          className="switch"
          role="switch"
          aria-checked={confirm}
          aria-label="确认后再保存"
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
          {confirm ? `提交 ${valid.length} 项，稍后确认` : `保存 ${valid.length} 项`}
        </button>
      </div>
    </div>
  );
}

export function LifeCapture() {
  return <LifeGate>{(life) => <Capture life={life} />}</LifeGate>;
}
