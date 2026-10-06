import type { ReactNode } from "react";
import { useApp } from "../lib/app";
import { WORKSPACE_STATUS } from "../lib/labels";
import { isLife, projectRows, zoneCounts } from "../lib/overview";
import { usePrefs, type Zone } from "../lib/prefs";
import { Link } from "../lib/router";
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
    { to: "/work/projects", label: "项目", icon: "folder", match: exact("/work/projects") },
  ];
}

const SHARED: NavItem[] = [
  { to: "/connections", label: "连接", icon: "plug", match: prefix("/connections") },
  { to: "/learn", label: "学习与发布", icon: "learn", match: prefix("/learn") },
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
    return <div className="meta bad">无法连接 EHAI 管理层</div>;
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
        {down.length ? `实时更新断开 · ${down.map((e) => e.workspace.workspace_id).join("、")}` : "实时更新已连接"}
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
  const projects = projectRows(overview.data).filter(
    (row) => !isLife(prefs.life, row.workspaceId, row.detail.project.project_id),
  );
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
        {zone === "work" && projects.length > 0 && (
          <nav className="nav-group" aria-label="项目">
            <div className="nav-label">项目</div>
            {projects.map(({ workspaceId, detail }) => {
              const to = `/work/w/${workspaceId}/projects/${detail.project.project_id}`;
              return (
                <Link
                  key={workspaceId + detail.project.project_id}
                  className="nav-item"
                  to={to}
                  aria-current={path === to ? "page" : undefined}
                >
                  <span className="grow">
                    {detail.project.name} <span className="meta mono">{workspaceId}</span>
                  </span>
                </Link>
              );
            })}
          </nav>
        )}
        <nav className="nav-group" aria-label="共用">
          <div className="nav-label">生活和工作共用</div>
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
