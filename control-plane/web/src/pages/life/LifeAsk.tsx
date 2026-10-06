import { useState } from "react";
import { ErrorBlock, QueryView, Spinner } from "../../components/Status";
import { core, type RoutingLabView } from "../../lib/api";
import { useApp, useQuery } from "../../lib/app";
import { byNewest } from "../../lib/overview";
import { Link } from "../../lib/router";
import { useSubmission } from "../../lib/submit";
import { LifeGate, type Life } from "./LifeGate";
import { takeDraft } from "./data";
import {
  FeedbackBar,
  LabPicker,
  requestAge,
  RouteLine,
  RoutingResult,
  useAutoAdvance,
  useChosenLab,
  useLabs,
} from "../shared/routing";

function Conversation({ life, lab }: { life: Life; lab: RoutingLabView }) {
  const { touch } = useApp();
  const labId = lab.lab.lab_id;
  const [message, setMessage] = useState(() => takeDraft());
  const submission = useSubmission("ask");
  const requests = useQuery(
    `routing-requests:${life.workspaceId}:${labId}`,
    () => core(life.workspaceId).listRoutingRequests(labId).then((r) => r.data),
    { workspaces: [life.workspaceId] },
  );
  const shown = byNewest([...(requests.data ?? [])], (r) => r.created_at)
    .filter((r) => r.case_role === "learning")
    .slice(0, 30)
    .reverse();
  const waiting = shown.some((r) => r.status === "queued" || r.status === "routing");
  useAutoAdvance(life.workspaceId, labId, waiting);

  async function ask() {
    const client = core(life.workspaceId);
    const result = await submission.run((key) =>
      client.submitRoutingRequest(labId, { idempotency_key: key, message: message.trim(), case_role: "learning" }),
    );
    if (result) {
      setMessage("");
      await client.advanceRoutingLab(labId).catch(() => undefined);
      touch(life.workspaceId);
    }
  }

  return (
    <>
      <QueryView query={requests}>
        {() =>
          shown.length === 0 ? (
            <p className="empty">还没有问过。快环只处理已批准的只读查询，其他请求会交给慢环。</p>
          ) : (
            <div className="section" style={{ gap: 20 }}>
              {shown.map((r) => (
                <div key={r.request_id} className="section" style={{ gap: 8 }}>
                  <div style={{ alignSelf: "flex-end", maxWidth: "85%" }}>
                    <div className="card" style={{ padding: "10px 14px", background: "var(--hover)", border: 0 }}>
                      <span className="pre">{r.message}</span>
                    </div>
                    <div className="meta" style={{ textAlign: "right" }}>
                      {requestAge(r)}
                    </div>
                  </div>
                  <div className="section" style={{ gap: 6 }}>
                    <RouteLine request={r} lab={lab} />
                    {(r.status === "queued" || r.status === "routing") && (
                      <span className="meta">
                        <Spinner /> 需要 Jev Connector 在运行
                      </span>
                    )}
                    {r.status === "escalated" && (
                      <span className="meta">处理好后会出现在这里；需要你决定的事会出现在「需要我处理」。</span>
                    )}
                    <RoutingResult request={r} />
                    <FeedbackBar workspaceId={life.workspaceId} request={r} />
                  </div>
                </div>
              ))}
            </div>
          )
        }
      </QueryView>
      <div className="composer sticky-composer">
        <textarea
          aria-label="问 EHAI"
          placeholder="比如：我还有哪些待办？"
          value={message}
          onChange={(e) => setMessage(e.target.value)}
        />
        <div className="composer-bar">
          <span className="meta" style={{ flex: "1 1 auto" }}>
            {lab.lab.mode === "shadow" ? "影子模式：只记录判断，不执行查询" : "只读：快环不会修改任何东西"}
          </span>
          <button
            type="button"
            className="btn small primary"
            disabled={submission.pending || !message.trim()}
            onClick={() => void ask()}
          >
            发送
          </button>
        </div>
      </div>
      {submission.error !== undefined && <ErrorBlock error={submission.error} />}
    </>
  );
}

function Ask({ life }: { life: Life }) {
  const labs = useLabs(life.workspaceId, life.projectId);
  const [lab, choose] = useChosenLab(`${life.workspaceId}:${life.projectId}`, labs.data);
  return (
    <div className="content">
      <div className="page-head">
        <div className="grow">
          <div className="meta">{life.projectName}</div>
          <h1 className="page-title">问 EHAI</h1>
        </div>
      </div>
      <QueryView query={labs}>
        {(data) =>
          lab === undefined ? (
            <div className="banner">
              <div className="banner-title">还没有接通快环</div>
              <div className="meta">
                「问」使用生活项目里的路由实验：先登记 Jev Connector，再创建实验并批准配方。步骤见{" "}
                <Link to="/learn">学习与发布</Link>。
              </div>
            </div>
          ) : (
            <>
              <LabPicker labs={data} chosen={lab} onChoose={choose} />
              <Conversation key={lab.lab.lab_id} life={life} lab={lab} />
            </>
          )
        }
      </QueryView>
    </div>
  );
}

export function LifeAsk() {
  return <LifeGate>{(life) => <Ask life={life} />}</LifeGate>;
}
