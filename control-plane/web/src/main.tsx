import { StrictMode, useEffect, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { Shell } from "./components/Shell";
import { ToastProvider } from "./components/Toast";
import { AppProvider } from "./lib/app";
import { applyTheme, getPrefs, setPrefs, type Zone } from "./lib/prefs";
import { Link, match, navigate, usePath } from "./lib/router";
import { LifeAsk } from "./pages/life/LifeAsk";
import { LifeCapture } from "./pages/life/LifeCapture";
import { LifeRecord, LifeRecords } from "./pages/life/LifeRecords";
import { LifeRoutineEdit, LifeRoutines } from "./pages/life/LifeRoutines";
import { LifeTasks } from "./pages/life/LifeTasks";
import { LifeToday } from "./pages/life/LifeToday";
import { ConnectionsPage } from "./pages/shared/Connections";
import { LearnPage } from "./pages/shared/Learn";
import { SettingsPage } from "./pages/shared/Settings";
import { ProjectPage, ProjectsPage } from "./pages/work/Projects";
import { RunPage } from "./pages/work/RunPage";
import { WorkHome } from "./pages/work/WorkHome";
import "./styles/tokens.css";
import "./styles/app.css";

type Route = { readonly zone: Zone | null; readonly page: ReactNode; readonly title: string };

function resolve(path: string): Route | "home" {
  if (path === "/") return "home";
  let p: Record<string, string> | null;
  if (path === "/life") return { zone: "life", page: <LifeToday />, title: "今天" };
  if (path === "/life/tasks") return { zone: "life", page: <LifeTasks />, title: "待办" };
  if (path === "/life/capture") return { zone: "life", page: <LifeCapture />, title: "录入" };
  if (path === "/life/routines") return { zone: "life", page: <LifeRoutines />, title: "定时" };
  if (path === "/life/routines/new") return { zone: "life", page: <LifeRoutineEdit routineId={null} />, title: "新建定时" };
  if ((p = match("/life/routines/:id", path)))
    return { zone: "life", page: <LifeRoutineEdit key={p["id"]} routineId={p["id"] as string} />, title: "编辑定时" };
  if (path === "/life/records") return { zone: "life", page: <LifeRecords />, title: "记录" };
  if ((p = match("/life/records/:id", path)))
    return { zone: "life", page: <LifeRecord key={p["id"]} workflowRunId={p["id"] as string} />, title: "记录" };
  if (path === "/life/ask") return { zone: "life", page: <LifeAsk />, title: "问" };
  if (path === "/work") return { zone: "work", page: <WorkHome />, title: "工作" };
  if (path === "/work/projects") return { zone: "work", page: <ProjectsPage />, title: "项目" };
  if ((p = match("/work/w/:ws/projects/:project", path)))
    return {
      zone: "work",
      page: <ProjectPage key={path} workspaceId={p["ws"] as string} projectId={p["project"] as string} />,
      title: "项目",
    };
  if ((p = match("/work/w/:ws/runs/:run", path)))
    return {
      zone: "work",
      page: <RunPage key={path} workspaceId={p["ws"] as string} runId={p["run"] as string} />,
      title: "Run",
    };
  if (path === "/connections") return { zone: null, page: <ConnectionsPage />, title: "连接" };
  if (path === "/learn") return { zone: null, page: <LearnPage />, title: "学习与发布" };
  if (path === "/settings") return { zone: null, page: <SettingsPage />, title: "设置" };
  return {
    zone: null,
    title: "找不到页面",
    page: (
      <div className="content">
        <h1 className="page-title">找不到页面</h1>
        <p>
          <Link to="/">回到首页</Link>
        </p>
      </div>
    ),
  };
}

function App() {
  const path = usePath();
  const route = resolve(path);

  useEffect(() => {
    // "/" opens the zone used last; the life zone is the default on first visit.
    if (route === "home") navigate(getPrefs().zone === "work" ? "/work" : "/life", { replace: true });
  }, [route]);

  const zone = route === "home" ? null : route.zone;
  useEffect(() => {
    if (zone !== null && zone !== getPrefs().zone) setPrefs({ zone });
  }, [zone]);

  useEffect(() => {
    if (route !== "home") document.title = `${route.title} · EHAI`;
  }, [route]);

  if (route === "home") return null;
  return (
    <Shell zone={route.zone ?? getPrefs().zone} path={path}>
      {route.page}
    </Shell>
  );
}

applyTheme(getPrefs().theme);

const root = document.getElementById("root");
if (root === null) throw new Error("missing #root");
createRoot(root).render(
  <StrictMode>
    <ToastProvider>
      <AppProvider>
        <App />
      </AppProvider>
    </ToastProvider>
  </StrictMode>,
);
