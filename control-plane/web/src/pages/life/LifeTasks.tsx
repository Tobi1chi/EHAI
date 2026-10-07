import { useState } from "react";
import { Icon } from "../../components/Icon";
import { QueryView } from "../../components/Status";
import type { LifeTask } from "../../lib/api";
import { dueBucket, type DueBucket } from "../../lib/format";
import { Link } from "../../lib/router";
import { LifeGate, type Life } from "./LifeGate";
import { sortByDue, TaskEditPanel, TaskRow, useLifeTasks } from "./data";

type Filter = LifeTask["status"];

const FILTERS: ReadonlyArray<{ value: Filter; label: string }> = [
  { value: "open", label: "未完成" },
  { value: "done", label: "已完成" },
  { value: "cancelled", label: "已取消" },
];

const GROUPS: ReadonlyArray<{ bucket: DueBucket; title: string }> = [
  { bucket: "overdue", title: "已过期" },
  { bucket: "today", title: "今天" },
  { bucket: "later", title: "之后" },
  { bucket: "none", title: "没定时间" },
];

function Tasks({ life }: { life: Life }) {
  const tasks = useLifeTasks(life);
  const [filter, setFilter] = useState<Filter>("open");
  const [editing, setEditing] = useState<LifeTask | null>(null);
  const now = new Date();

  return (
    <div className="content">
      <div className="page-head">
        <div className="grow">
          <div className="meta">{life.projectName}</div>
          <h1 className="page-title">待办</h1>
        </div>
        <Link className="btn" to="/life/capture">
          <Icon name="plus" size={16} strokeWidth={2.2} />
          录入
        </Link>
      </div>
      <div className="segmented" role="tablist" aria-label="筛选">
        {FILTERS.map((f) => (
          <button
            key={f.value}
            type="button"
            role="tab"
            aria-selected={filter === f.value}
            onClick={() => setFilter(f.value)}
          >
            {f.label}
            {tasks.data ? ` ${tasks.data.filter((t) => t.status === f.value).length}` : ""}
          </button>
        ))}
      </div>
      <QueryView query={tasks}>
        {(data) => {
          const shown = sortByDue(data.filter((t) => t.status === filter));
          if (shown.length === 0) return <p className="empty">{filter === "open" ? "都做完了。" : `没有${FILTERS.find((f) => f.value === filter)?.label}的待办。`}</p>;
          if (filter !== "open") {
            return (
              <div className="list">
                {shown.reverse().map((task) => (
                  <TaskRow key={task.task_id} life={life} task={task} onOpen={setEditing} />
                ))}
              </div>
            );
          }
          return GROUPS.map(({ bucket, title }) => {
            const rows = shown.filter((t) => dueBucket(t.due_at, now) === bucket);
            if (rows.length === 0) return null;
            return (
              <section key={bucket} className="section" style={{ marginBottom: 16 }}>
                <h2 className={`meta${bucket === "overdue" ? " bad" : ""}`} style={{ fontWeight: 600 }}>
                  {title}
                </h2>
                <div className="list">
                  {rows.map((task) => (
                    <TaskRow key={task.task_id} life={life} task={task} onOpen={setEditing} />
                  ))}
                </div>
              </section>
            );
          });
        }}
      </QueryView>
      <Link className="fab" to="/life/capture">
        <Icon name="plus" size={18} strokeWidth={2.2} />
        录入
      </Link>
      {editing && <TaskEditPanel life={life} task={editing} onClose={() => setEditing(null)} />}
    </div>
  );
}

export function LifeTasks() {
  return <LifeGate>{(life) => <Tasks life={life} />}</LifeGate>;
}
