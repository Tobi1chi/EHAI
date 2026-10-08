import { useState } from "react";
import { Icon } from "../../components/Icon";
import { ErrorBlock, QueryView } from "../../components/Status";
import { useToast } from "../../components/Toast";
import { core, errorStatus, type LifeRoutine } from "../../lib/api";
import { useApp, useQuery } from "../../lib/app";
import {
  dateTime,
  fromLocalInput,
  intervalText,
  splitInterval,
  timeZoneLabel,
  toLocalInput,
  UNIT_LABEL,
  UNIT_SECONDS,
  when,
  type IntervalUnit,
} from "../../lib/format";
import { Link, navigate } from "../../lib/router";
import { useSubmission } from "../../lib/submit";
import { LifeGate, type Life } from "./LifeGate";
import { SchedulerBanner } from "./LifeToday";
import { useRoutines, useScheduler } from "./data";

function EnabledSwitch({ life, routine }: { life: Life; routine: LifeRoutine }) {
  const { touch } = useApp();
  const toast = useToast();
  const submission = useSubmission("routine-enabled");
  async function toggle() {
    const result = await submission.run((key) =>
      core(life.workspaceId).updateRoutine(routine.routine_id, {
        idempotency_key: key,
        expected_version: routine.version,
        name: routine.name,
        interval_seconds: routine.interval_seconds,
        next_due_at: routine.next_due_at,
        enabled: !routine.enabled,
      }),
    );
    touch(life.workspaceId);
    if (!result) toast(errorStatus(submission.lastError()) === 409 ? "这个定时刚在别处改过，已刷新" : "没保存上，再试一次");
  }
  return (
    <button
      type="button"
      className="switch"
      role="switch"
      aria-checked={routine.enabled}
      aria-label={`开启 ${routine.name}`}
      disabled={submission.pending}
      onClick={() => void toggle()}
    >
      <span />
    </button>
  );
}

function Routines({ life }: { life: Life }) {
  const routines = useRoutines(life);
  const scheduler = useScheduler(life);
  return (
    <div className="content">
      <div className="page-head">
        <div className="grow">
          <div className="meta">{life.projectName}</div>
          <h1 className="page-title">定时</h1>
        </div>
        <Link className="btn" to="/life/records">
          记录
        </Link>
        <Link className="btn" to="/life/routines/new">
          <Icon name="plus" size={16} strokeWidth={2.2} />
          新建
        </Link>
      </div>
      <SchedulerBanner status={scheduler.data} />
      {scheduler.data?.active && (
        <div className="meta" style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className="dot" style={{ background: "var(--ok)" }} />
          定时正常运行
        </div>
      )}
      <QueryView query={routines}>
        {(data) =>
          data.length === 0 ? (
            <p className="empty">还没有定时。可以新建一个，定期回顾一下待办。</p>
          ) : (
            <div className="section" style={{ gap: 12 }}>
              {data.map((r) => (
                <div key={r.routine_id} className="card">
                  <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
                    <Link
                      to={`/life/routines/${r.routine_id}`}
                      style={{ flex: "1 1 auto", minWidth: 0, textDecoration: "none", display: "flex", flexDirection: "column" }}
                    >
                      <span className="title" style={{ fontWeight: 600, color: r.enabled ? undefined : "var(--text-2)" }}>
                        {r.name}
                      </span>
                      <span className="meta">回顾待办 · {intervalText(r.interval_seconds)}</span>
                    </Link>
                    <EnabledSwitch life={life} routine={r} />
                  </div>
                  <dl className="kv">
                    <dt>下次</dt>
                    <dd className={r.enabled ? undefined : "meta"}>{r.enabled ? when(r.next_due_at) : "已关闭"}</dd>
                    <dt>上次</dt>
                    <dd>
                      {r.last_workflow_run_id ? <Link to={`/life/records/${r.last_workflow_run_id}`}>查看回顾</Link> : "还没运行过"}
                    </dd>
                  </dl>
                </div>
              ))}
            </div>
          )
        }
      </QueryView>
      <p className="meta">目前只能按固定间隔回顾待办。</p>
    </div>
  );
}

function defaultNext(): string {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  d.setHours(9, 0, 0, 0);
  return toLocalInput(d.toISOString());
}

function RoutineForm({ life, routine }: { life: Life; routine: LifeRoutine | null }) {
  const { touch } = useApp();
  const toast = useToast();
  const initial = routine ? splitInterval(routine.interval_seconds) : { amount: 7, unit: "days" as IntervalUnit };
  const [name, setName] = useState(routine?.name ?? "每周生活回顾");
  const [amount, setAmount] = useState(initial.amount);
  const [unit, setUnit] = useState<IntervalUnit>(initial.unit);
  const [next, setNext] = useState(routine ? toLocalInput(routine.next_due_at) : defaultNext());
  const [enabled, setEnabled] = useState(routine?.enabled ?? true);
  const submission = useSubmission(routine ? "routine-update" : "routine-create");
  const conflict = errorStatus(submission.error) === 409 && routine !== null;
  const seconds = amount * UNIT_SECONDS[unit];
  const nextIso = fromLocalInput(next);
  const inRange = seconds >= 60 && seconds <= 31536000;

  async function save() {
    if (nextIso === null) return;
    const body = { name: name.trim(), interval_seconds: seconds, next_due_at: nextIso, enabled };
    const client = core(life.workspaceId);
    const result = await submission.run((key) =>
      routine
        ? client.updateRoutine(routine.routine_id, { ...body, idempotency_key: key, expected_version: routine.version })
        : client.createRoutine(life.projectId, { ...body, idempotency_key: key }),
    );
    if (result) {
      touch(life.workspaceId);
      toast(routine ? "已保存" : "已新建定时");
      navigate("/life/routines");
    }
  }

  return (
    <div className="content">
      <div className="page-head">
        <div className="grow">
          <div className="meta">
            <Link to="/life/routines">定时</Link>
          </div>
          <h1 className="page-title">{routine ? "编辑定时" : "新建定时"}</h1>
        </div>
      </div>
      {conflict && (
        <div className="banner attn" role="alert">
          <div className="banner-title">这个定时刚在别处改过</div>
          <div className="meta">重新载入后再改，免得覆盖掉。</div>
          <div>
            <button type="button" className="btn small" onClick={() => window.location.reload()}>
              重新载入
            </button>
          </div>
        </div>
      )}
      <label className="field">
        <span>名称</span>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <div className="field">
        <span>做什么</span>
        <div className="card" style={{ padding: 12 }}>
          <span>回顾待办</span>
          <span className="meta">列出还没做完和已经过期的待办，不会改动它们。</span>
        </div>
      </div>
      <div className="split">
        <label className="field">
          <span>每隔</span>
          <input
            className="input"
            type="number"
            min={1}
            value={amount}
            onChange={(e) => setAmount(Math.max(1, Math.floor(Number(e.target.value) || 1)))}
          />
        </label>
        <label className="field">
          <span>单位</span>
          <select className="select" value={unit} onChange={(e) => setUnit(e.target.value as IntervalUnit)}>
            {(Object.keys(UNIT_LABEL) as IntervalUnit[]).map((u) => (
              <option key={u} value={u}>
                {UNIT_LABEL[u]}
              </option>
            ))}
          </select>
        </label>
      </div>
      {!inRange && <p className="meta bad">间隔要在 1 分钟到 365 天之间。</p>}
      <label className="field">
        <span>{routine ? "下次运行" : "第一次运行"}</span>
        <input className="input" type="datetime-local" value={next} onChange={(e) => setNext(e.target.value)} />
        <span className="meta">{timeZoneLabel()}</span>
      </label>
      <label className="row" style={{ cursor: "pointer" }}>
        <span className="grow">
          <span className="title">开启</span>
          <span className="meta">{enabled ? "到时间就回顾一次。" : "先保存，不运行。"}</span>
        </span>
        <button
          type="button"
          className="switch"
          role="switch"
          aria-checked={enabled}
          aria-label="开启"
          onClick={() => setEnabled((v) => !v)}
        >
          <span />
        </button>
      </label>
      <p className="meta">
        {nextIso ? `从 ${dateTime(nextIso)} 开始，${intervalText(seconds)}一次。` : "填一下第一次运行的时间。"}
        电脑关机期间错过的，开机后只补一次。
      </p>
      {!conflict && submission.error !== undefined && <ErrorBlock error={submission.error} />}
      <div className="actions">
        <button type="button" className="btn" onClick={() => navigate("/life/routines")}>
          取消
        </button>
        <button
          type="button"
          className="btn primary"
          disabled={submission.pending || conflict || !name.trim() || !inRange || nextIso === null}
          onClick={() => void save()}
        >
          保存
        </button>
      </div>
    </div>
  );
}

function EditRoutine({ life, routineId }: { life: Life; routineId: string }) {
  const routine = useQuery(
    `routine:${life.workspaceId}:${routineId}`,
    () => core(life.workspaceId).getRoutine(routineId).then((r) => r.data),
  );
  // Read once: live refreshes must not overwrite fields while the user is editing.
  return <QueryView query={routine}>{(data) => <RoutineForm key={data.routine_id} life={life} routine={data} />}</QueryView>;
}

export function LifeRoutines() {
  return <LifeGate>{(life) => <Routines life={life} />}</LifeGate>;
}

export function LifeRoutineEdit({ routineId }: { routineId: string | null }) {
  return (
    <LifeGate>
      {(life) => (routineId ? <EditRoutine life={life} routineId={routineId} /> : <RoutineForm life={life} routine={null} />)}
    </LifeGate>
  );
}
