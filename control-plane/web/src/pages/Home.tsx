import { useEffect, useRef, useState } from "react";
import { manager, type AttentionItem, type InboxKind } from "../lib/api";
import { useApp, useQuery } from "../lib/app";
import { useLifeBinding } from "../lib/life-binding";
import { ago, when } from "../lib/format";
import { assistantIssue, attentionNote, attentionState, inboxQuestion } from "../lib/labels";
import { Link } from "../lib/router";
import { useSubmission } from "../lib/submit";
import { ErrorBlock, QueryView } from "../components/Status";
import { RefCard } from "../components/RefCard";
import { DecisionCard } from "../components/DecisionCard";
import { INBOX_KIND } from "../lib/labels";
import { queueLink } from "../lib/notifications";

function QueueRow({ item, title, expandable }: { item: AttentionItem; title: string; expandable: boolean }) {
  const decisions = item.kind === "inbox" && item.state in INBOX_KIND
    ? [{ kind: item.state as InboxKind, request_id: item.object_id }] : item.decisions ?? [];
  const row = <>
    <span className={`chip ${item.label}`}>{item.label === "life" ? "生活" : "工作"}</span>
    <span className="grow"><span className="title">{title}</span><span className="meta">{attentionState(item.kind, item.state)}</span></span>
    <span className="meta">{item.at ? item.kind === "life_task" && item.state !== "done" ? when(item.at) : ago(item.at) : "未定时间"}</span>
  </>;
  if (!expandable || !decisions.length) return <Link className="row attention-row" to={queueLink(item)}>{row}</Link>;
  return <details className="queue-decisions"><summary className="row attention-row">{row}</summary>
    <div className="section">{decisions.map(decision => <DecisionCard key={decision.kind + decision.request_id}
      target={{ workspaceId: item.workspace_id, kind: decision.kind, requestId: decision.request_id }} />)}</div>
    <Link to={queueLink(item)}>查看所属页面</Link>
  </details>;
}

function Assistant() {
  const { overview } = useApp();
  const query = useQuery("assistant", () => manager.getAssistant().then(r => r.data), {
    workspaces: (overview.data?.workspaces ?? []).map(e => e.workspace.workspace_id),
  });
  const [text, setText] = useState("");
  const submission = useSubmission("assistant-message");
  const view = query.data;
  const messages = useRef<HTMLDivElement>(null);
  const messageList = useRef<HTMLDivElement>(null);
  const followBottom = useRef(true);
  const lastScrollTop = useRef(0);
  function scrollToBottom() {
    const area = messages.current;
    if (!area) return;
    area.scrollTop = area.scrollHeight;
    lastScrollTop.current = area.scrollTop;
  }
  useEffect(() => {
    if (!view?.busy) return;
    const timer = window.setInterval(query.reload, 2000);
    return () => window.clearInterval(timer);
  }, [view?.busy, query.reload]);
  const messageKey = view?.messages.map(m => `${m.message_id}:${m.outcome}`).join(",");
  useEffect(() => {
    if (followBottom.current) scrollToBottom();
  }, [messageKey, view?.busy]);
  useEffect(() => {
    const content = messageList.current;
    if (!content) return;
    // Live cards finish reading after the answer renders. Follow their growth only
    // while at the bottom; let the user scroll back through older answers.
    const observer = new ResizeObserver(() => {
      if (followBottom.current) scrollToBottom();
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, [view !== undefined]);
  async function send() {
    followBottom.current = true;
    const result = await submission.run(() => manager.sendAssistantMessage({ text: text.trim() }));
    if (result) setText("");
    query.reload();
  }
  async function reset() {
    await submission.run(() => manager.resetAssistant());
    query.reload();
  }
  return <section className="home-assistant card section" aria-label="助手">
    <div className="section-head"><h2 className="h2 grow">助手</h2>
      <button className="btn small" disabled={!view?.available || view.busy || submission.pending} onClick={() => void reset()}>清空对话</button>
    </div>
    {view?.model && <p className="meta">{view.harness === "codex" ? "Codex" : "Pi"} · {view.model} · 只读助手</p>}
    <QueryView query={query} plain>{data => <>
      {!data.available && <p className="banner">{assistantIssue(data.unavailable_reason, true)}</p>}
      <div className="assistant-messages" aria-live="polite" ref={messages} onScroll={() => {
        const area = messages.current;
        if (!area) return;
        if (area.scrollHeight - area.scrollTop - area.clientHeight < 30) {
          followBottom.current = true;
        } else if (area.scrollTop < lastScrollTop.current) {
          // Only scrolling back disables following. Card growth and our own
          // scroll events can leave a gap without any user interaction.
          followBottom.current = false;
        }
        lastScrollTop.current = area.scrollTop;
      }}>
        <div className="assistant-message-list" ref={messageList}>
        {data.messages.length === 0 && data.available && <p className="empty">问问手上的事怎样了。助手只读，操作由你点击。</p>}
        {data.messages.map(message => <article key={message.message_id} className={`assistant-message ${message.role}`}>
          <div className="meta">{message.role === "user" ? "你" : "助手"} · {ago(message.created_at)}</div>
          {message.outcome === "failed" ? <p className="bad" role="alert">{assistantIssue(message.error)}</p> : <p className="body pre">{message.text}</p>}
          {message.refs.map((ref, index) => <RefCard key={`${ref.kind}:${ref.workspace_id}:${ref.object_id}:${index}`} reference={ref} />)}
        </article>)}
        {data.busy && <p className="meta" role="status">正在查看你的事情，请稍候…</p>}
        </div>
      </div>
      <form className="section" onSubmit={event => { event.preventDefault(); void send(); }}>
        <label className="field"><span>问助手</span><textarea className="textarea" maxLength={4000} value={text}
          disabled={!data.available || data.busy || submission.pending} onChange={event => setText(event.target.value)} placeholder="手上的事怎样了？" /></label>
        <div className="actions"><button className="btn primary" disabled={!data.available || data.busy || submission.pending || !text.trim()}>发送</button></div>
      </form>
    </>}</QueryView>
    {submission.error !== undefined && <ErrorBlock error={submission.error} plain />}
  </section>;
}

export function Home() {
  const { overview } = useApp();
  const life = useLifeBinding();
  const workspaceIds = (overview.data?.workspaces ?? []).map(e => e.workspace.workspace_id);
  const attention = useQuery(`attention:${life.binding?.workspaceId ?? ""}:${life.binding?.projectId ?? ""}:${life.loading}`,
    () => manager.getAttention().then(r => r.data), { workspaces: workspaceIds, intervalMs: 30000 });
  function queueTitle(item: AttentionItem): string {
    if (item.kind !== "inbox") return item.title;
    const inbox = overview.data?.workspaces.find(e => e.workspace.workspace_id === item.workspace_id)?.inbox?.items
      .find(entry => entry.request_id === item.object_id && entry.kind === item.state);
    return inbox ? inboxQuestion(inbox) : "待处理事项";
  }
  function workspaceName(id: string): string {
    const workspace = overview.data?.workspaces.find(e => e.workspace.workspace_id === id)?.workspace;
    return workspace?.path.split(/[\\/]/).filter(Boolean).pop() || id;
  }
  return <div className="content wide home">
    <div className="page-head"><div><h1 className="page-title">首页</h1><p className="meta">生活与工作，都在这里。</p></div></div>
    <div className="home-columns">
      <section className="home-queue section" aria-label="注意力队列">
        <QueryView query={attention} plain>{data => <>
          {data.unavailable.map(entry => <p className="banner attn" key={entry.workspace_id}>工作区 {workspaceName(entry.workspace_id)} 暂时读不到，可能已停止。</p>)}
          {data.notes.map((note, i) => <p className="meta" key={i}>{attentionNote(note)}</p>)}
          {(["needs_you", "in_progress", "done"] as const).map((group, i) => <section className="card section" key={group}>
            <h2 className="h2">{["需要你处理", "进行中", "已完成"][i]} <span className="meta">{data[group].length}</span></h2>
            {data[group].length === 0 ? <p className="empty">暂无</p> : <div className="list">{data[group].map(item => <QueueRow key={`${item.workspace_id}:${item.kind}:${item.object_id}`} item={item} title={queueTitle(item)} expandable={group === "needs_you"} />)}</div>}
            {group === "done" && data.done_truncated && <p className="meta">仅显示最近七天的前 20 项。</p>}
          </section>)}
          <p className="meta">更新于 {ago(data.observed_at)} · 各工作区分别读取</p>
        </>}</QueryView>
      </section>
      <Assistant />
    </div>
  </div>;
}
