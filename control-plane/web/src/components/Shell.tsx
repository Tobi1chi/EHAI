import type { ReactNode } from "react";
import { useApp } from "../lib/app";
import { THREAD_PHASE, WORKSPACE_STATUS } from "../lib/labels";
import { useLifeBinding } from "../lib/life-binding";
import { Link } from "../lib/router";
import { useQueueNotifications } from "../lib/notifications";
import { threadPath, useThreads, workThreadRows } from "../lib/threads";
import { Icon } from "./Icon";

type NavItem = {
  readonly to: string;
  readonly label: string;
  readonly icon: Parameters<typeof Icon>[0]["name"];
  readonly count?: number;
  readonly match: (path: string) => boolean;
};

const exact = (to: string) => (path: string) => path === to;
const prefix = (to: string) => (path: string) => path === to || path.startsWith(to + "/");

function lifeItems(count: number): NavItem[] {
  return [
    { to: "/life", label: "今天", icon: "today", count, match: exact("/life") },
    { to: "/life/tasks", label: "待办", icon: "tasks", match: prefix("/life/tasks") },
    { to: "/life/routines", label: "定时", icon: "clock", match: prefix("/life/routines") },
    { to: "/life/records", label: "记录", icon: "record", match: prefix("/life/records") },
  ];
}

function workItems(count: number): NavItem[] {
  return [
    { to: "/work", label: "首页", icon: "home", count, match: exact("/work") },
    { to: "/work/new", label: "新线程", icon: "plus", match: exact("/work/new") },
    { to: "/work/projects", label: "项目", icon: "folder", match: exact("/work/projects") },
  ];
}

const SIDEBAR_THREADS = 30;

const PHASE_DOT: Record<keyof typeof THREAD_PHASE, string> = {
  needs_attention: "var(--attn)",
  running: "var(--ok)",
  paused: "var(--text-3)",
  awaiting_approval: "var(--attn)",
  planning: "var(--text-2)",
  completed: "var(--border-strong)",
  failed: "var(--bad)",
  idle: "var(--border-strong)",
};

const SHARED: NavItem[] = [
  { to: "/connections", label: "连接", icon: "plug", match: prefix("/connections") },
];

function NavLink({ item, path }: { item: NavItem; path: string }) {
  return (
    <Link className="nav-item" to={item.to} aria-current={item.match(path) ? "page" : undefined}>
      <span className="icon">
        <Icon name={item.icon} />
      </span>
      <span className="grow">{item.label}</span>
      {item.count !== undefined && item.count > 0 && <span className="nav-count">{item.count}</span>}
    </Link>
  );
}

function LiveFooter() {
  const { overview, live } = useApp();
  const entries = overview.data?.workspaces ?? [];
  if (overview.error !== undefined && overview.data === undefined) {
    return <div className="meta bad">连不上 EHAI</div>;
  }
  const available = entries.filter((e) => e.available);
  const down = available.filter((e) => live[e.workspace.workspace_id] === "down");
  const offline = entries.filter((e) => !e.available);
  return (
    <div className="meta" style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span
          className="dot"
          style={{ background: down.length ? "var(--bad)" : available.length ? "var(--ok)" : "var(--text-3)" }}
        />
        {down.length ? `${down.map((e) => e.workspace.workspace_id).join("、")} 断开了，内容可能不是最新的` : "已连接"}
      </span>
      {offline.length > 0 && (
        <span>
          {offline.map((e) => `${e.workspace.workspace_id} ${WORKSPACE_STATUS[e.workspace.status]}`).join(" · ")}
        </span>
      )}
    </div>
  );
}

export function Shell({ path, children }: { path: string; children: ReactNode }) {
  const { binding } = useLifeBinding();
  const threads = useThreads();
  useQueueNotifications();
  const threadRows = workThreadRows(threads.data, binding).slice(0, SIDEBAR_THREADS);
  const items: NavItem[] = [
    { to: "/", label: "首页", icon: "home", match: exact("/") },
    ...workItems(0).map((i) => i.to === "/work" ? { ...i, label: "工作线程" } : i),
    ...lifeItems(0),
    { to: "/life/capture", label: "录入", icon: "plus", match: exact("/life/capture") },
    ...SHARED,
    { to: "/settings", label: "设置", icon: "settings", match: exact("/settings") },
  ];
  return <div className="shell">
    <aside className="sidebar">
      <Link className="brand" to="/"><span className="brand-mark">E</span>EHAI</Link>
      <nav className="nav-group" aria-label="主导航">{items.map(item => <NavLink key={item.to} item={item} path={path} />)}</nav>
      {threadRows.length > 0 && <nav className="nav-group" aria-label="线程">
        {threadRows.map(row => <Link key={threadPath(row)} className="nav-item sub thread-item" to={threadPath(row)}>
          <span className="dot" style={{ background: PHASE_DOT[row.thread.phase] }} />
          <span className="grow">{row.thread.objective}</span>
        </Link>)}
      </nav>}
      <div className="sidebar-foot"><LiveFooter /></div>
    </aside>
    <div className="main"><details className="mobile-navigation">
      <summary>导航</summary><nav aria-label="主导航">{items.map(item => <NavLink key={item.to} item={item} path={path} />)}</nav>
    </details>{children}</div>
  </div>;
}
