import { useState } from "react";
import { ErrorBlock, QueryView, Spinner } from "../../components/Status";
import { useToast } from "../../components/Toast";
import {
  core,
  type ConnectorConnection,
  type RoutingLabView,
  type RoutingRecipe,
  type RoutingRecipeInput,
  type RoutingReplay,
  type RoutingRequest,
} from "../../lib/api";
import { useApp, useQuery } from "../../lib/app";
import { ago } from "../../lib/format";
import { RECIPE_TARGET, routingReason } from "../../lib/labels";
import { byNewest, projectRows } from "../../lib/overview";
import { actorName, setPrefs, usePrefs } from "../../lib/prefs";
import { useSubmission } from "../../lib/submit";
import { FEEDBACK_LABEL, RouteLine, RoutingResult, useAutoAdvance } from "./routing";

const FALLBACK_STATUS: Readonly<Record<string, string>> = { running: "处理中", completed: "处理完了", failed: "失败" };

type LabRef = {
  readonly workspaceId: string;
  readonly projectId: string;
  readonly projectName: string;
  readonly view: RoutingLabView;
};

type ProjectRef = { readonly workspaceId: string; readonly projectId: string; readonly projectName: string };

function useAllLabs() {
  const { overview } = useApp();
  const projects: ProjectRef[] = projectRows(overview.data).map((r) => ({
    workspaceId: r.workspaceId,
    projectId: r.detail.project.project_id,
    projectName: r.detail.project.name,
  }));
  const key = projects.map((p) => p.workspaceId + ":" + p.projectId).join(",");
  const workspaces = [...new Set(projects.map((p) => p.workspaceId))];
  const labs = useQuery(
    overview.data ? `all-labs:${key}` : null,
    () =>
      Promise.all(
        projects.map((p) =>
          core(p.workspaceId)
            .listRoutingLabs(p.projectId)
            .then((r) => r.data.map((view): LabRef => ({ ...p, view }))),
        ),
      ).then((lists) => lists.flat()),
    { workspaces },
  );
  return { labs, projects };
}

export function LearnPage() {
  const prefs = usePrefs();
  const { labs, projects } = useAllLabs();
  const chosenId = prefs.labs["learn"];
  const all = labs.data ?? [];
  const chosen = all.find((l) => l.view.lab.lab_id === chosenId) ?? all[0];

  return (
    <div className="content wide">
      <div className="page-head">
        <div className="grow">
          <div className="meta">通用</div>
          <h1 className="page-title">学习与发布</h1>
        </div>
      </div>
      <p className="body muted">
        常见的问题由快环按你发布过的配方直接回答，其余的交给慢环。慢环处理多了会提出新配方，回放检查通过后由你决定是否发布。
      </p>
      <QueryView query={labs}>
        {() =>
          chosen === undefined ? (
            <CreateLab projects={projects} />
          ) : (
            <>
              {all.length > 1 && (
                <label className="field">
                  <span>实验</span>
                  <select
                    className="select"
                    value={chosen.view.lab.lab_id}
                    onChange={(e) => setPrefs({ labs: { ...prefs.labs, learn: e.target.value } })}
                  >
                    {all.map((l) => (
                      <option key={l.view.lab.lab_id} value={l.view.lab.lab_id}>
                        {l.workspaceId} / {l.projectName} / {l.view.lab.name}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <Lab key={chosen.view.lab.lab_id} lab={chosen} />
              <details className="disclosure">
                <summary>新建实验</summary>
                <div style={{ marginTop: 12 }}>
                  <CreateLab projects={projects} />
                </div>
              </details>
            </>
          )
        }
      </QueryView>
    </div>
  );
}

function Lab({ lab }: { lab: LabRef }) {
  const { touch } = useApp();
  const ws = lab.workspaceId;
  const labId = lab.view.lab.lab_id;
  const client = core(ws);
  const opts = { workspaces: [ws] };
  const view = useQuery(`lab:${ws}:${labId}`, () => client.getRoutingLab(labId).then((r) => r.data), opts);
  const metrics = useQuery(`lab-metrics:${ws}:${labId}`, () => client.getRoutingMetrics(labId).then((r) => r.data), opts);
  const requests = useQuery(
    `routing-requests:${ws}:${labId}`,
    () => client.listRoutingRequests(labId).then((r) => r.data),
    opts,
  );
  const [advancing, setAdvancing] = useState(false);
  const current = view.data ?? lab.view;
  const all = requests.data ?? [];
  const waiting = all.some((r) => r.status === "queued" || r.status === "routing");
  useAutoAdvance(ws, labId, waiting);

  // A paused recipe goes back through replay and publish, the same way as a new one.
  const candidates = current.recipes.filter((r) => r.status === "candidate" || r.status === "paused");
  const published = current.recipes.filter((r) => r.status === "active");
  const escalated = all.filter((r) => r.status === "escalated");

  async function advance() {
    setAdvancing(true);
    await client.advanceRoutingLab(labId).catch(() => undefined);
    setAdvancing(false);
    touch(ws);
  }

  return (
    <>
      <div className="card">
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span className="h2" style={{ flex: "1 1 auto" }}>
            {current.lab.name}
          </span>
          <span className="tag">{current.lab.mode === "shadow" ? "试运行（只记录不回答）" : "正式回答（只读）"}</span>
          <span className="tag">慢环：{current.lab.fallback_mode === "pi" ? "Pi" : "外部 Agent"}</span>
          <button type="button" className="btn small" disabled={advancing} onClick={() => void advance()}>
            {advancing ? <Spinner /> : "手动处理一轮"}
          </button>
        </div>
        <div className="meta">
          {lab.workspaceId} / {lab.projectName} · 目录版本 {current.lab.catalog_version} · 阈值：置信度 ≥{" "}
          {current.lab.confidence_threshold}，概率 ≥ {current.lab.probability_threshold}
        </div>
        {metrics.data && (
          <div className="stats">
            <div className="stat">
              <strong>{metrics.data.request_count}</strong>
              <span className="meta">请求</span>
            </div>
            <div className="stat">
              <strong>{metrics.data.fast_completed_count}</strong>
              <span className="meta">快环完成</span>
            </div>
            <div className="stat">
              <strong>{metrics.data.escalated_count}</strong>
              <span className="meta">交给慢环</span>
            </div>
            <div className="stat">
              <strong>{metrics.data.system2_resolved_count}</strong>
              <span className="meta">慢环已处理</span>
            </div>
            <div className="stat">
              <strong className={metrics.data.misroute_count ? "bad" : undefined}>{metrics.data.misroute_count}</strong>
              <span className="meta">纠错</span>
            </div>
          </div>
        )}
      </div>

      <section className="section">
        <div className="section-head">
          <h2 className="h2">待发布</h2>
          {candidates.length > 0 && <span className="nav-count">{candidates.length}</span>}
        </div>
        {candidates.length === 0 ? (
          <p className="empty">没有待发布的配方。</p>
        ) : (
          candidates.map((recipe) => (
            <Candidate key={recipe.recipe_id} lab={lab} view={current} recipe={recipe} requests={all} />
          ))
        )}
      </section>

      <section className="section">
        <div className="section-head">
          <h2 className="h2">交给慢环的请求</h2>
          {escalated.length > 0 && <span className="nav-count">{escalated.length}</span>}
        </div>
        {escalated.length === 0 ? (
          <p className="empty">没有在等慢环的请求。</p>
        ) : (
          <>
            {escalated.map((r) => (
              <Escalated key={r.request_id} workspaceId={ws} request={r} />
            ))}
          </>
        )}
        <ProposeCandidate workspaceId={ws} labId={labId} requests={all} />
      </section>

      <section className="section">
        <h2 className="h2">在用的配方</h2>
        {published.length === 0 ? (
          <p className="empty">还没有在用的配方。</p>
        ) : (
          published.map((recipe) => (
            <Published key={recipe.recipe_id} workspaceId={ws} recipe={recipe} hits={metrics.data?.by_recipe[recipe.recipe_id]} />
          ))
        )}
      </section>

      <section className="section">
        <h2 className="h2">所有请求</h2>
        <SubmitCase workspaceId={ws} labId={labId} />
        <QueryView query={requests}>
          {(data) => (
            <details className="disclosure">
              <summary>全部 {data.length} 条</summary>
              <div className="list" style={{ marginTop: 8 }}>
                {[...data].reverse().map((r) => (
                  <div key={r.request_id} className="row" style={{ alignItems: "flex-start" }}>
                    <span className="grow">
                      <span>「{r.message}」</span>
                      <RouteLine request={r} lab={current} />
                      <span className="meta">
                        {r.case_role === "validation" ? "验证案例" : "学习"} · {ago(r.created_at)}
                        {r.feedback ? ` · ${FEEDBACK_LABEL[r.feedback.outcome]}` : ""}
                      </span>
                    </span>
                  </div>
                ))}
              </div>
            </details>
          )}
        </QueryView>
      </section>
    </>
  );
}

function Candidate({
  lab,
  view,
  recipe,
  requests,
}: {
  lab: LabRef;
  view: RoutingLabView;
  recipe: RoutingRecipe;
  requests: ReadonlyArray<RoutingRequest>;
}) {
  const ws = lab.workspaceId;
  const prefs = usePrefs();
  const { touch } = useApp();
  const toast = useToast();
  const labReplays = useQuery(
    `routing-replays:${ws}:${view.lab.lab_id}`,
    () => core(ws).listRoutingReplays(view.lab.lab_id).then((r) => r.data),
    { workspaces: [ws] },
  );
  const replays = byNewest(
    (labReplays.data ?? []).filter((r) => r.candidate_id === recipe.recipe_id),
    (r) => r.created_at,
  );
  const passed = replays.find((r) => r.status === "passed" && r.catalog_version === view.lab.catalog_version);
  // A protocol stand-in is not Jev evidence; the core refuses to publish on it unless told so.
  const trial = passed?.entries.some((e) => e.judgement?.evidence_source === "protocol_trial") ?? false;
  const [allowTrial, setAllowTrial] = useState(false);
  // Replay cases are judged by the Jev Connector; keep advancing while one is pending.
  useAutoAdvance(ws, view.lab.lab_id, replays.some((r) => r.status === "pending"));
  const publish = useSubmission("publish-recipe");

  async function doPublish(replay: RoutingReplay) {
    const result = await publish.run((key) =>
      core(ws).publishRoutingRecipe(recipe.recipe_id, {
        idempotency_key: key,
        actor: actorName(prefs),
        replay_id: replay.replay_id,
        allow_protocol_trial: trial && allowTrial,
      }),
    );
    if (result) {
      touch(ws);
      toast(`已发布「${recipe.name}」`);
    }
  }

  const sources = requests.filter((r) => recipe.source_request_ids.includes(r.request_id));
  return (
    <div className="card">
      <div>
        <div className={recipe.status === "paused" ? "meta attn" : "meta"}>
          {recipe.status === "paused"
            ? `已暂停${recipe.pause_reason ? `：${recipe.pause_reason}` : ""} · 回放通过后可以重新发布`
            : `新配方 · 根据 ${recipe.source_request_ids.length} 条请求提出`}
        </div>
        <div className="h2">{recipe.name}</div>
      </div>
      <p className="body">{recipe.applicability}</p>
      <div className="tags">
        <span className="tag">执行 {recipe.target}</span>
        <span className="tag">{RECIPE_TARGET[recipe.target] ?? recipe.target}</span>
        <span className="tag">只读</span>
      </div>
      {sources.length > 0 && (
        <div className="meta">
          来源：{sources.map((r) => `「${r.message}」`).join(" ")}
        </div>
      )}
      {replays.map((replay) => (
        <ReplayView
          key={replay.replay_id}
          replay={replay}
          view={view}
          current={replay.catalog_version === view.lab.catalog_version}
        />
      ))}
      {labReplays.error !== undefined && <ErrorBlock error={labReplays.error} onRetry={labReplays.reload} />}
      <StartReplay
        workspaceId={ws}
        labId={view.lab.lab_id}
        recipe={recipe}
        view={view}
        requests={requests}
        onStarted={labReplays.reload}
      />
      {trial && (
        <label className="row" style={{ alignItems: "flex-start", minHeight: 0 }}>
          <input className="check" type="checkbox" checked={allowTrial} onChange={(e) => setAllowTrial(e.target.checked)} />
          <span className="grow">
            <span>仍然发布</span>
            <span className="meta attn">这次回放是测试用的模拟 Jev 判的，不算数。只在试用时勾选。</span>
          </span>
        </label>
      )}
      {publish.error !== undefined && <ErrorBlock error={publish.error} />}
      <div className="actions">
        <button
          type="button"
          className="btn primary"
          disabled={!passed || publish.pending || (trial && !allowTrial)}
          title={passed ? undefined : "先跑一次回放，通过后才能发布"}
          onClick={() => passed && void doPublish(passed)}
        >
          {recipe.status === "paused" ? "重新发布" : "发布"}
        </button>
      </div>
    </div>
  );
}

/** A recipe's name for a replay choice; the choices are recipe IDs or "escalate". */
function choiceName(view: RoutingLabView, choice: string): string {
  if (choice === "escalate") return "交给慢环";
  const recipe = view.recipes.find((r) => r.recipe_id === choice);
  return recipe ? `「${recipe.name}」` : choice.slice(0, 8);
}

function ReplayView({ replay, view, current }: { replay: RoutingReplay; view: RoutingLabView; current: boolean }) {
  const tone = replay.status === "passed" ? "ok" : replay.status === "failed" ? "bad" : "";
  const done = replay.entries.filter((e) => e.passed !== null).length;
  return (
    <details className="disclosure">
      <summary>
        <span className={tone}>
          回放 {replay.status === "pending" ? "进行中" : replay.status === "passed" ? "通过" : "未通过"} · {done}/
          {replay.entries.length}
        </span>
        {!current && <span className="meta">（配方有变动，这次回放作废）</span>}
      </summary>
      <div className="list" style={{ marginTop: 8 }}>
        {replay.entries.map((e) => (
          <div key={e.request_id} className="row" style={{ minHeight: 40 }}>
            <span className="grow">
              <span>「{e.message}」</span>
              <span className="meta">
                {e.case_role === "validation" ? "验证" : "学习"} · 应该选{choiceName(view, e.expected_choice)}
                {e.judgement ? ` · Jev 选了${choiceName(view, e.judgement.choice)}` : ""}
                {e.reason ? ` · ${routingReason(e.reason)}` : ""}
              </span>
            </span>
            <span className={`tag ${e.passed === true ? "ok" : e.passed === false ? "bad" : ""}`}>
              {e.passed === null ? "等待" : e.passed ? "通过" : "未通过"}
            </span>
          </div>
        ))}
      </div>
    </details>
  );
}

function StartReplay({
  workspaceId,
  labId,
  recipe,
  view,
  requests,
  onStarted,
}: {
  workspaceId: string;
  labId: string;
  recipe: RoutingRecipe;
  view: RoutingLabView;
  requests: ReadonlyArray<RoutingRequest>;
  onStarted: () => void;
}) {
  const { touch } = useApp();
  const [open, setOpen] = useState(false);
  const [cases, setCases] = useState<Record<string, string>>({});
  const submission = useSubmission("start-replay");
  const choices = [
    { value: recipe.recipe_id, label: `这个配方「${recipe.name}」` },
    ...view.recipes.filter((r) => r.status === "active").map((r) => ({ value: r.recipe_id, label: r.name })),
    { value: "escalate", label: "交给慢环" },
  ];
  const selected = Object.entries(cases).filter(([, choice]) => choice);

  async function start() {
    const result = await submission.run((key) =>
      core(workspaceId).startRoutingReplay(labId, {
        idempotency_key: key,
        candidate_id: recipe.recipe_id,
        cases: selected.map(([request_id, expected_choice]) => ({ request_id, expected_choice })),
      }),
    );
    if (result) {
      onStarted();
      setOpen(false);
      await core(workspaceId).advanceRoutingLab(labId).catch(() => undefined);
      touch(workspaceId);
    }
  }

  if (!open) {
    return (
      <div>
        <button type="button" className="btn small" onClick={() => setOpen(true)}>
          开始回放
        </button>
      </div>
    );
  }
  return (
    <div className="card" style={{ background: "var(--sidebar)" }}>
      <div className="h2">选几条请求来回放</div>
      <p className="meta">
        至少选两条不同的请求，其中一条是没用来提出这个配方的验证案例。回放只看 Jev 怎么选，不会真的执行，也不会把正确答案告诉 Jev。
      </p>
      <div className="list">
        {requests.map((r) => (
          <div key={r.request_id} className="row" style={{ minHeight: 44 }}>
            <span className="grow">
              <span>「{r.message}」</span>
              <span className="meta">
                {r.case_role === "validation" ? "验证案例" : "学习"}
                {recipe.source_request_ids.includes(r.request_id) ? " · 用于提出候选" : ""}
              </span>
            </span>
            <select
              className="select"
              style={{ width: 180 }}
              aria-label={`「${r.message}」应该怎么处理`}
              value={cases[r.request_id] ?? ""}
              onChange={(e) => setCases((c) => ({ ...c, [r.request_id]: e.target.value }))}
            >
              <option value="">不选</option>
              {choices.map((c) => (
                <option key={c.value} value={c.value}>
                  {c.label}
                </option>
              ))}
            </select>
          </div>
        ))}
      </div>
      {submission.error !== undefined && <ErrorBlock error={submission.error} />}
      <div className="actions">
        <button type="button" className="btn" onClick={() => setOpen(false)}>
          取消
        </button>
        <button
          type="button"
          className="btn primary"
          disabled={submission.pending || selected.length < 2}
          onClick={() => void start()}
        >
          回放 {selected.length} 条
        </button>
      </div>
    </div>
  );
}

function Escalated({ workspaceId, request }: { workspaceId: string; request: RoutingRequest }) {
  const prefs = usePrefs();
  const { touch } = useApp();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [response, setResponse] = useState("");
  const [evidence, setEvidence] = useState("");
  const submission = useSubmission("resolve-routing");
  const running = request.fallback?.status === "running";

  async function resolve() {
    const result = await submission.run((key) =>
      core(workspaceId).resolveRoutingRequest(request.request_id, {
        idempotency_key: key,
        actor: actorName(prefs),
        response: response.trim(),
        evidence: evidence.trim(),
      }),
    );
    if (result) {
      touch(workspaceId);
      toast("已记下处理结果");
    }
  }

  return (
    <div className="card">
      <div>
        <div className="body">「{request.message}」</div>
        <div className="meta">
          {routingReason(request.reason)} · {request.case_role === "validation" ? "验证案例" : "学习"} · {ago(request.created_at)}
        </div>
      </div>
      {request.fallback && (
        <div className="meta">
          Pi：{FALLBACK_STATUS[request.fallback.status] ?? request.fallback.status}
          {request.fallback.error ? ` · ${request.fallback.error}` : ""}
          {request.fallback.change_reason ? ` · ${request.fallback.change_reason}` : ""}
          {request.fallback.project_change ? ` · 已转成项目修改（${request.fallback.project_change.status}）` : ""}
        </div>
      )}
      {request.result && <RoutingResult request={request} />}
      {!open ? (
        <div>
          <button type="button" className="btn small" disabled={running} onClick={() => setOpen(true)}>
            记下处理结果
          </button>
        </div>
      ) : (
        <>
          <label className="field">
            <span>回复</span>
            <textarea className="textarea" value={response} onChange={(e) => setResponse(e.target.value)} />
          </label>
          <label className="field">
            <span>依据</span>
            <input className="input" value={evidence} onChange={(e) => setEvidence(e.target.value)} />
          </label>
          <p className="meta">会标记为未经核实。</p>
          {submission.error !== undefined && <ErrorBlock error={submission.error} />}
          <div className="actions">
            <button type="button" className="btn" onClick={() => setOpen(false)}>
              取消
            </button>
            <button
              type="button"
              className="btn primary"
              disabled={submission.pending || !response.trim() || !evidence.trim()}
              onClick={() => void resolve()}
            >
              提交
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function ProposeCandidate({
  workspaceId,
  labId,
  requests,
}: {
  workspaceId: string;
  labId: string;
  requests: ReadonlyArray<RoutingRequest>;
}) {
  const { touch } = useApp();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [applicability, setApplicability] = useState("");
  const [target, setTarget] = useState<RoutingRecipeInput["target"]>("life.tasks.list");
  const [sources, setSources] = useState<string[]>([]);
  const submission = useSubmission("propose-recipe");
  const learning = requests.filter((r) => r.case_role === "learning");

  async function propose() {
    const result = await submission.run((key) =>
      core(workspaceId).proposeRoutingRecipe(labId, {
        idempotency_key: key,
        recipe: { name: name.trim(), applicability: applicability.trim(), target },
        source_request_ids: sources,
      }),
    );
    if (result) {
      touch(workspaceId);
      setOpen(false);
      toast("已提出，回放通过后就能发布");
    }
  }

  if (!open) {
    return (
      <div>
        <button type="button" className="btn small ghost" disabled={learning.length === 0} onClick={() => setOpen(true)}>
          手动提一个配方
        </button>
      </div>
    );
  }
  return (
    <div className="card" style={{ background: "var(--sidebar)" }}>
      <div className="h2">提一个配方</div>
      <p className="meta">一般由慢环提出。在这里手动提的，也一样要回放、发布。</p>
      <label className="field">
        <span>名称</span>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="field">
        <span>什么时候用</span>
        <textarea className="textarea" value={applicability} onChange={(e) => setApplicability(e.target.value)} />
      </label>
      <label className="field">
        <span>做什么</span>
        <select className="select" value={target} onChange={(e) => setTarget(e.target.value as RoutingRecipeInput["target"])}>
          <option value="life.tasks.list">life.tasks.list · 查看生活待办</option>
          <option value="inbox.list">inbox.list · 查看待处理</option>
        </select>
      </label>
      <div className="field">
        <span>根据哪些请求（学习案例，最多 20 条）</span>
        <div className="list">
          {learning.map((r) => (
            <label key={r.request_id} className="row" style={{ minHeight: 40, cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={sources.includes(r.request_id)}
                onChange={(e) =>
                  setSources((s) => (e.target.checked ? [...s, r.request_id] : s.filter((id) => id !== r.request_id)))
                }
              />
              <span className="grow">「{r.message}」</span>
            </label>
          ))}
        </div>
      </div>
      {submission.error !== undefined && <ErrorBlock error={submission.error} />}
      <div className="actions">
        <button type="button" className="btn" onClick={() => setOpen(false)}>
          取消
        </button>
        <button
          type="button"
          className="btn primary"
          disabled={submission.pending || !name.trim() || !applicability.trim() || sources.length === 0 || sources.length > 20}
          onClick={() => void propose()}
        >
          提交
        </button>
      </div>
    </div>
  );
}

const RECIPE_HIT: Readonly<Record<string, string>> = { selected: "选中", reviewed: "已反馈", misroutes: "走错" };

function Published({
  workspaceId,
  recipe,
  hits,
}: {
  workspaceId: string;
  recipe: RoutingRecipe;
  hits: Readonly<Record<string, number>> | undefined;
}) {
  const prefs = usePrefs();
  const { touch } = useApp();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const submission = useSubmission("pause-recipe");

  async function pause() {
    const result = await submission.run((key) =>
      core(workspaceId).pauseRoutingRecipe(recipe.recipe_id, {
        idempotency_key: key,
        actor: actorName(prefs),
        reason: reason.trim(),
      }),
    );
    if (result) {
      touch(workspaceId);
      toast(`已暂停「${recipe.name}」`);
    }
  }

  const hitText = hits ? Object.entries(hits).map(([k, v]) => `${RECIPE_HIT[k] ?? k} ${v}`).join(" · ") : "";
  return (
    <div className="card" style={{ gap: 8 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span className="h2" style={{ flex: "1 1 auto", color: recipe.status === "paused" ? "var(--text-2)" : undefined }}>
          {recipe.name}
        </span>
        <span className={`tag ${recipe.status === "paused" ? "attn" : "ok"}`}>{recipe.status === "paused" ? "已暂停" : "已发布"}</span>
      </div>
      <div className="meta">
        {recipe.target} · {recipe.approved_by ? `由 ${recipe.approved_by} 批准` : ""}
        {hitText ? ` · ${hitText}` : ""}
      </div>
      <div className="body muted">{recipe.applicability}</div>
      {recipe.status === "paused" && recipe.pause_reason && (
        <div className="meta attn">
          暂停原因：{recipe.pause_reason}
          {recipe.paused_by ? `（${recipe.paused_by}）` : ""}
        </div>
      )}
      {recipe.status === "active" &&
        (open ? (
          <>
            <label className="field">
              <span>暂停原因</span>
              <input className="input" value={reason} onChange={(e) => setReason(e.target.value)} />
            </label>
            {submission.error !== undefined && <ErrorBlock error={submission.error} />}
            <div className="actions">
              <button type="button" className="btn" onClick={() => setOpen(false)}>
                取消
              </button>
              <button
                type="button"
                className="btn primary"
                disabled={submission.pending || !reason.trim()}
                onClick={() => void pause()}
              >
                暂停配方
              </button>
            </div>
          </>
        ) : (
          <div>
            <button type="button" className="btn small" onClick={() => setOpen(true)}>
              暂停
            </button>
          </div>
        ))}
    </div>
  );
}

function SubmitCase({ workspaceId, labId }: { workspaceId: string; labId: string }) {
  const { touch } = useApp();
  const [message, setMessage] = useState("");
  const [role, setRole] = useState<"learning" | "validation">("validation");
  const submission = useSubmission("routing-case");

  async function submit() {
    const client = core(workspaceId);
    const result = await submission.run((key) =>
      client.submitRoutingRequest(labId, { idempotency_key: key, message: message.trim(), case_role: role }),
    );
    if (result) {
      setMessage("");
      await client.advanceRoutingLab(labId).catch(() => undefined);
      touch(workspaceId);
    }
  }

  return (
    <div className="composer">
      <textarea
        aria-label="写一条请求"
        placeholder="写一条请求。验证案例只用来回放检查"
        value={message}
        onChange={(e) => setMessage(e.target.value)}
      />
      <div className="composer-bar">
        <div className="segmented" role="tablist" aria-label="案例角色">
          <button type="button" role="tab" aria-selected={role === "validation"} onClick={() => setRole("validation")}>
            验证案例
          </button>
          <button type="button" role="tab" aria-selected={role === "learning"} onClick={() => setRole("learning")}>
            学习
          </button>
        </div>
        <span style={{ flex: "1 1 auto" }} />
        <button
          type="button"
          className="btn small primary"
          disabled={submission.pending || !message.trim()}
          onClick={() => void submit()}
        >
          提交
        </button>
      </div>
      {submission.error !== undefined && <ErrorBlock error={submission.error} />}
    </div>
  );
}

const DEFAULT_RECIPES: ReadonlyArray<RoutingRecipeInput> = [
  {
    name: "查看生活待办",
    applicability: "用户只想查看已有生活待办，不要求新建、修改或完成任务",
    target: "life.tasks.list",
  },
  {
    name: "查看待处理",
    applicability: "用户只想知道当前有哪些等待自己处理的事项，不要求做出决定",
    target: "inbox.list",
  },
];

function CreateLab({ projects }: { projects: ReadonlyArray<ProjectRef> }) {
  const prefs = usePrefs();
  const { touch } = useApp();
  const toast = useToast();
  const workspaces = [...new Set(projects.map((p) => p.workspaceId))];
  const connectors = useQuery(
    projects.length ? `jev-connectors:${projects.map((p) => p.projectId).join(",")}` : null,
    () =>
      Promise.all(
        projects.map((p) =>
          core(p.workspaceId)
            .listConnectors(p.projectId)
            .then((r) => r.data.filter((c) => c.manifest.connector_type === "jev").map((c) => ({ project: p, connector: c }))),
        ),
      ).then((lists) => lists.flat()),
    { workspaces },
  );
  const [choice, setChoice] = useState("");
  const [name, setName] = useState("生活只读试验");
  const [mode, setMode] = useState<"shadow" | "read_only">("read_only");
  const [picked, setPicked] = useState<string[]>(DEFAULT_RECIPES.map((r) => r.target));
  const submission = useSubmission("create-lab");
  const options = connectors.data ?? [];
  const selected: { project: ProjectRef; connector: ConnectorConnection } | undefined =
    options.find((o) => o.connector.connector_id === choice) ?? options[0];

  async function create() {
    if (!selected) return;
    const result = await submission.run((key) =>
      core(selected.project.workspaceId).createRoutingLab(selected.project.projectId, {
        idempotency_key: key,
        name: name.trim(),
        connector_id: selected.connector.connector_id,
        mode,
        actor: actorName(prefs),
        approved_recipes: DEFAULT_RECIPES.filter((r) => picked.includes(r.target)),
      }),
    );
    if (result) {
      touch(selected.project.workspaceId);
      setPrefs({ labs: { ...prefs.labs, learn: result.data.lab.lab_id } });
      toast("已创建实验");
    }
  }

  return (
    <div className="section">
      <QueryView query={connectors}>
        {() =>
          options.length === 0 ? (
            <div className="banner">
              <div className="banner-title">先连上 Jev</div>
              <div className="meta">
                在终端里运行下面的命令。私有目录不要放在代码仓库里，密钥放在私有文件里。
              </div>
              <div className="code">
                {`uv run ehai-jev init --api-url ${window.location.origin}/workspaces/<工作区> --project-id <项目 ID> --state-dir <私有目录> --model jev-latest\nuv run ehai-jev run --state-dir <私有目录> --key-file <私有密钥文件>`}
              </div>
              <div className="meta">登记好再回到这里。项目 ID 可以在「连接」页找到。</div>
            </div>
          ) : (
            <div className="card">
              <div className="h2">新建路由实验</div>
              <label className="field">
                <span>Jev 连接</span>
                <select
                  className="select"
                  value={selected?.connector.connector_id ?? ""}
                  onChange={(e) => setChoice(e.target.value)}
                >
                  {options.map((o) => (
                    <option key={o.connector.connector_id} value={o.connector.connector_id}>
                      {o.project.workspaceId} / {o.project.projectName} / {o.connector.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>名称</span>
                <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
              </label>
              <div className="field">
                <span>模式</span>
                <div className="segmented" role="tablist">
                  <button type="button" role="tab" aria-selected={mode === "read_only"} onClick={() => setMode("read_only")}>
                    正式回答
                  </button>
                  <button type="button" role="tab" aria-selected={mode === "shadow"} onClick={() => setMode("shadow")}>
                    试运行（只记录不回答）
                  </button>
                </div>
              </div>
              <div className="field">
                <span>先发布这些配方</span>
                {DEFAULT_RECIPES.map((r) => (
                  <label key={r.target} className="row" style={{ minHeight: 40, cursor: "pointer" }}>
                    <input
                      type="checkbox"
                      checked={picked.includes(r.target)}
                      onChange={(e) =>
                        setPicked((p) => (e.target.checked ? [...p, r.target] : p.filter((t) => t !== r.target)))
                      }
                    />
                    <span className="grow">
                      <span>{r.name}</span>
                      <span className="meta">{r.applicability}</span>
                    </span>
                  </label>
                ))}
              </div>
              {submission.error !== undefined && <ErrorBlock error={submission.error} />}
              <div className="actions">
                <button
                  type="button"
                  className="btn primary"
                  disabled={submission.pending || !name.trim() || picked.length === 0}
                  onClick={() => void create()}
                >
                  创建
                </button>
              </div>
            </div>
          )
        }
      </QueryView>
    </div>
  );
}
