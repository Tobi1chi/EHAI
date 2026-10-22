import type { ReactNode } from "react";
import { useApp } from "../lib/app";
import { THREAD_PHASE, WORKSPACE_STATUS } from "../lib/labels";
import { zoneCounts } from "../lib/overview";
import { usePrefs, type Zone } from "../lib/prefs";
import { Link } from "../lib/router";
import { useThreadNotifications } from "../lib/notifications";
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
    { to: "/life/ask", label: "问", icon: "ask", match: prefix("/life/ask") },
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
  { to: "/answers", label: "自动回答", icon: "learn", match: (path) => prefix("/answers")(path) || prefix("/learn")(path) },
];

function ZoneSwitch({ zone, counts }: { zone: Zone; counts: { life: number; work: number } }) {
  return (
    <nav className="zones" aria-label="分区">
      <Link className="zone" to="/life" aria-current={zone === "life" ? "true" : undefined}>
        生活
        {counts.life > 0 && <span className="nav-count">{counts.life}</span>}
      </Link>
      <Link className="zone" to="/work" aria-current={zone === "work" ? "true" : undefined}>
        工作
        {counts.work > 0 && <span className="nav-count">{counts.work}</span>}
      </Link>
    </nav>
  );
}

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

export function Shell({ zone, path, children }: { zone: Zone; path: string; children: ReactNode }) {
  const { overview } = useApp();
  const prefs = usePrefs();
  const counts = zoneCounts(overview.data, prefs.life);
  const items = zone === "life" ? lifeItems(counts.life) : workItems(counts.work);
  const threads = useThreads();
  useThreadNotifications(threads.data);
  const threadRows = zone === "work" ? workThreadRows(threads.data, prefs.life).slice(0, SIDEBAR_THREADS) : [];
  const tabs: NavItem[] =
    zone === "life"
      ? lifeItems(counts.life).filter((i) => i.to !== "/life/records")
      : [...workItems(counts.work), ...SHARED];

  return (
    <div className="shell">
      <aside className="sidebar">
        <Link className="brand" to={zone === "life" ? "/life" : "/work"}>
          <span className="brand-mark">E</span>
          EHAI
        </Link>
        <ZoneSwitch zone={zone} counts={counts} />
        <nav className="nav-group" aria-label={zone === "life" ? "生活" : "工作"}>
          {items.map((item) => (
            <NavLink key={item.to} item={item} path={path} />
          ))}
        </nav>
        {zone === "work" && threadRows.length > 0 && (
          <nav className="nav-group" aria-label="线程">
            <div className="nav-label">线程</div>
            {threadRows.map((row) => {
              const to = threadPath(row);
              return (
                <Link
                  key={to}
                  className="nav-item sub thread-item"
                  to={to}
                  title={`${row.thread.objective}\n${row.thread.project_name} · ${THREAD_PHASE[row.thread.phase]}`}
                  aria-current={path === to ? "page" : undefined}
                >
                  <span className="dot" style={{ background: PHASE_DOT[row.thread.phase], marginTop: 7 }} />
                  <span className="grow">{row.thread.objective}</span>
                </Link>
              );
            })}
          </nav>
        )}
        <nav className="nav-group" aria-label="共用">
          <div className="nav-label">通用</div>
          {SHARED.map((item) => (
            <NavLink key={item.to} item={item} path={path} />
          ))}
          <NavLink item={{ to: "/settings", label: "设置", icon: "settings", match: prefix("/settings") }} path={path} />
        </nav>
        <div className="sidebar-foot">
          <LiveFooter />
        </div>
      </aside>
      <div className="main">
        <div className="mobile-top">
          <ZoneSwitch zone={zone} counts={counts} />
          <span style={{ flex: "1 1 auto" }} />
          <Link className="icon-btn" to="/settings" aria-label="设置">
            <Icon name="settings" />
          </Link>
        </div>
        {children}
      </div>
      <nav className="tabbar" aria-label="主导航">
        {tabs.map((item) => (
          <Link key={item.to} to={item.to} aria-current={item.match(path) ? "page" : undefined}>
            <Icon name={item.icon} size={22} />
            {item.label}
            {item.count !== undefined && item.count > 0 && <span className="tab-dot" />}
          </Link>
        ))}
      </nav>
    </div>
  );
}
