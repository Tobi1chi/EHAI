import { useState } from "react";
import { Icon } from "../../components/Icon";
import { InboxList } from "../../components/InboxList";
import { InboxPanel, type InboxTarget } from "../../components/InboxPanel";
import { ErrorBlock } from "../../components/Status";
import type { LifeTask, RoutineSchedulerStatus } from "../../lib/api";
import { useApp } from "../../lib/app";
import { ago, dueBucket, intervalText, longDate, when } from "../../lib/format";
import { byNewest, inboxRows, isLife, zoneCounts } from "../../lib/overview";
import { usePrefs } from "../../lib/prefs";
import { Link, navigate } from "../../lib/router";
import { LifeGate, type Life } from "./LifeGate";
import { setDraft, sortByDue, TaskEditPanel, TaskRow, useLifeTasks, useRoutines, useScheduler } from "./data";

export function SchedulerBanner({ status }: { status: RoutineSchedulerStatus | undefined }) {
  if (status === undefined || status.active) return null;
  return (
    <div className="banner bad" role="alert">
      <div className="banner-title">
        <span className="bad">
          <Icon name="alert" size={16} strokeWidth={2.2} />
        </span>
        定时停了
      </div>
      <div className="meta">
        {status.last_tick_at ? `上次运行是${ago(status.last_tick_at)}。` : "还没运行过。"}
        停着的时候不会回顾待办，恢复后错过的只补一次。
      </div>
      {status.last_error && <div className="code">{status.last_error}</div>}
    </div>
  );
}

const WEEK_MS = 7 * 86400 * 1000;

function Today({ life }: { life: Life }) {
  const { overview } = useApp();
  const prefs = usePrefs();
  const tasks = useLifeTasks(life);
  const routines = useRoutines(life);
  const scheduler = useScheduler(life);
  const [target, setTarget] = useState<InboxTarget | null>(null);
  const [editing, setEditing] = useState<LifeTask | null>(null);
  const [text, setText] = useState("");
  const now = new Date();

  const inbox = byNewest(
    inboxRows(overview.data).filter((row) => isLife(prefs.life, row.workspaceId, row.item.owner.project_id)),
    (row) => row.item.created_at,
  );
  const workCount = zoneCounts(overview.data, prefs.life).work;
  const open = (tasks.data ?? []).filter((t) => t.status === "open");
  const today = sortByDue(open.filter((t) => ["overdue", "today"].includes(dueBucket(t.due_at, now))));
  const horizon = now.getTime() + WEEK_MS;
  const upcoming = [
    ...open
      .filter((t) => dueBucket(t.due_at, now) === "later" && new Date(t.due_at as string).getTime() < horizon)
      .map((t) => ({ key: "t" + t.task_id, at: t.due_at as string, title: t.title, meta: "待办", to: "/life/tasks" })),
    ...(routines.data ?? [])
      .filter((r) => r.enabled && new Date(r.next_due_at).getTime() < horizon)
      .map((r) => ({
        key: "r" + r.routine_id,
        at: r.next_due_at,
        title: r.name,
        meta: `回顾待办 · ${intervalText(r.interval_seconds)}`,
        to: `/life/routines/${r.routine_id}`,
      })),
  ].sort((a, b) => Date.parse(a.at) - Date.parse(b.at));

  function carry(to: string) {
    setDraft(text.trim());
    navigate(to);
  }

  return (
    <div className="content">
      <div className="page-head">
        <div className="grow">
          <div className="meta">{longDate(now)}</div>
          <h1 className="page-title">今天</h1>
        </div>
      </div>

      <SchedulerBanner status={scheduler.data} />

      <div className="composer">
        <textarea
          aria-label="记一件事"
          placeholder="记一件事，或者问 EHAI"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <div className="composer-bar">
          <span style={{ flex: "1 1 auto" }} />
          <button type="button" className="btn small ghost" onClick={() => carry("/life/ask")} disabled={!text.trim()}>
            问 EHAI
          </button>
          <button type="button" className="btn small primary" onClick={() => carry("/life/capture")}>
            记成待办
          </button>
        </div>
      </div>

      <section className="section" aria-labelledby="today-inbox">
        <div className="section-head">
          <h2 id="today-inbox" className="h2">
            待处理
          </h2>
          {inbox.length > 0 && <span className="nav-count">{inbox.length}</span>}
          <span className="grow" />
          {workCount > 0 && (
            <Link className="meta" to="/work">
              工作那边还有 {workCount} 项
            </Link>
          )}
        </div>
        <InboxList rows={inbox} onOpen={setTarget} showWorkspace={false} />
      </section>

      <section className="section" aria-labelledby="today-tasks">
        <div className="section-head">
          <h2 id="today-tasks" className="h2 grow">
            今天的待办
          </h2>
          <Link className="meta" to="/life/tasks">
            全部 {open.length} 项
          </Link>
        </div>
        {tasks.error !== undefined && <ErrorBlock error={tasks.error} onRetry={tasks.reload} stale={tasks.data !== undefined} />}
        {tasks.data !== undefined &&
          (today.length === 0 ? (
            <p className="empty">今天没有要到期的。</p>
          ) : (
            <div className="list">
              {today.map((task) => (
                <TaskRow key={task.task_id} life={life} task={task} onOpen={setEditing} />
              ))}
            </div>
          ))}
      </section>

      <section className="section" aria-labelledby="today-upcoming">
        <h2 id="today-upcoming" className="h2">
          未来 7 天
        </h2>
        {upcoming.length === 0 ? (
          <p className="empty">接下来一周没有安排。</p>
        ) : (
          <div className="list">
            {upcoming.map((u) => (
              <Link key={u.key} className="row" to={u.to}>
                <span className="grow">
                  <span>{u.title}</span>
                  <span className="meta">{u.meta}</span>
                </span>
                <span className="meta">{when(u.at, now)}</span>
              </Link>
            ))}
          </div>
        )}
      </section>

      <Link className="fab" to="/life/capture">
        <Icon name="plus" size={18} strokeWidth={2.2} />
        录入
      </Link>
      {target && <InboxPanel target={target} onClose={() => setTarget(null)} />}
      {editing && <TaskEditPanel life={life} task={editing} onClose={() => setEditing(null)} />}
    </div>
  );
}

export function LifeToday() {
  return <LifeGate>{(life) => <Today life={life} />}</LifeGate>;
}
