import { useEffect, useLayoutEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { Icon } from "../../components/Icon";
import type { Attempt, PlanGraph, PlanNode } from "../../lib/api";
import { duration } from "../../lib/format";
import { NODE_KIND, NODE_STATUS } from "../../lib/labels";
import { Link } from "../../lib/router";
import { attemptPath } from "./AttemptPage";

export type Trail = { readonly workspaceId: string; readonly runId: string; readonly attempts: ReadonlyArray<Attempt> };

const NODE_W = 232;
const NODE_H = 64;
const COL_GAP = 56;
const ROW_GAP = 16;
const PHASE_GAP = 24;
const PAD = 20;
const HEADER = 36;
/** A waypoint for an edge that skips columns; it keeps its own row so the edge never runs behind a node. */
const LANE = 12;
const MAX_HEIGHT = 420;
const MIN_SCALE = 0.25;
const MAX_SCALE = 1.5;

type Box = { x: number; y: number };
type Header = { title: string; x: number; width: number };
type Layout = {
  width: number;
  height: number;
  boxes: Map<string, Box>;
  /** Lane rows (left end, vertical centre) for each edge that skips columns. */
  routes: Map<string, Box[]>;
  headers: Header[];
};

const lane = (edgeId: string, k: number) => `lane:${edgeId}:${k}`;
const isLane = (id: string) => id.startsWith("lane:");

/**
 * Columns by longest path from the roots, left to right. A phase starts right of the
 * previous phase, so each phase is a run of columns under one header.
 */
function layout(plan: PlanGraph): Layout {
  const ids = plan.nodes.map((n) => n.plan_node_id);
  const known = new Set(ids);
  const edges = plan.edges.filter(
    (e) => known.has(e.source_node_id) && known.has(e.target_node_id) && e.source_node_id !== e.target_node_id,
  );
  const parents = new Map<string, string[]>(ids.map((id) => [id, []]));
  for (const e of edges) parents.get(e.target_node_id)?.push(e.source_node_id);
  const phaseOf = new Map<string, number>();
  plan.phases.forEach((p, i) => p.node_ids.forEach((id) => phaseOf.set(id, i)));

  const members = plan.phases.map((p) => p.node_ids.filter((id) => known.has(id)));
  const depth = new Map<string, number>();
  const visiting = new Set<string>();
  // The last column of phase i (and everything before it); -1 before the first phase.
  const phaseEnd = (i: number): number =>
    i < 0 ? -1 : Math.max(phaseEnd(i - 1), ...(members[i] ?? []).map(level));
  function level(id: string): number {
    const seen = depth.get(id);
    if (seen !== undefined) return seen;
    if (visiting.has(id)) return 0; // a cycle should not exist; do not loop on one
    visiting.add(id);
    const phase = phaseOf.get(id);
    const d = Math.max(
      Math.max(-1, ...(parents.get(id) ?? []).map(level)) + 1,
      phase === undefined ? 0 : phaseEnd(phase - 1) + 1,
    );
    visiting.delete(id);
    depth.set(id, d);
    return d;
  }
  ids.forEach(level);

  const count = Math.max(0, ...depth.values()) + 1;
  const columns: string[][] = Array.from({ length: count }, () => []);
  ids.forEach((id) => columns[depth.get(id) ?? 0]?.push(id));
  // Ordering uses the lane before a node as its parent when an edge skips columns.
  const before = new Map<string, string[]>(ids.map((id) => [id, []]));
  for (const e of edges) {
    const from = depth.get(e.source_node_id) ?? 0;
    const to = depth.get(e.target_node_id) ?? 0;
    let previous = e.source_node_id;
    for (let d = from + 1; d < to; d += 1) {
      const id = lane(e.edge_id, d);
      columns[d]?.push(id);
      before.set(id, [previous]);
      previous = id;
    }
    if (to > from) before.get(e.target_node_id)?.push(previous);
  }
  const position = new Map<string, number>();
  for (const column of columns) {
    const centre = (id: string) => {
      const ps = (before.get(id) ?? []).map((p) => position.get(p)).filter((v): v is number => v !== undefined);
      return ps.length ? ps.reduce((a, b) => a + b, 0) / ps.length : 0;
    };
    column.sort((a, b) => centre(a) - centre(b) || ids.indexOf(a) - ids.indexOf(b));
    let offset = 0;
    const sizes = column.map((id) => (isLane(id) ? LANE : NODE_H));
    const total = sizes.reduce((a, b) => a + b, 0) + Math.max(0, column.length - 1) * ROW_GAP;
    column.forEach((id, i) => {
      position.set(id, offset - total / 2 + (sizes[i] ?? 0) / 2);
      offset += (sizes[i] ?? 0) + ROW_GAP;
    });
  }

  // Phases get a header, and a little more room between them.
  const columnPhase = columns.map((c) => {
    const real = c.find((id) => !isLane(id));
    return real === undefined ? undefined : phaseOf.get(real);
  });
  const top = PAD + (plan.phases.length ? HEADER : 0);
  const half = Math.max(NODE_H / 2, ...[...position.entries()].map(([id, p]) => Math.abs(p) + (isLane(id) ? LANE : NODE_H) / 2));
  const middle = top + half;
  const boxes = new Map<string, Box>();
  const lanes = new Map<string, Box>();
  const headers: Header[] = [];
  let x = PAD;
  columns.forEach((column, i) => {
    const phase = columnPhase[i];
    if (i > 0) x += NODE_W + COL_GAP + (phase !== columnPhase[i - 1] ? PHASE_GAP : 0);
    if (phase !== undefined) {
      const last = headers[headers.length - 1];
      if (i > 0 && phase === columnPhase[i - 1] && last) last.width = x + NODE_W - last.x;
      else headers.push({ title: plan.phases[phase]?.title ?? "", x, width: NODE_W });
    }
    for (const id of column) {
      const centre = middle + (position.get(id) ?? 0);
      if (isLane(id)) lanes.set(id, { x, y: centre });
      else boxes.set(id, { x, y: centre - NODE_H / 2 });
    }
  });
  const routes = new Map<string, Box[]>();
  for (const e of edges) {
    const from = depth.get(e.source_node_id) ?? 0;
    const to = depth.get(e.target_node_id) ?? 0;
    const points: Box[] = [];
    for (let d = from + 1; d < to; d += 1) {
      const point = lanes.get(lane(e.edge_id, d));
      if (point) points.push(point);
    }
    routes.set(e.edge_id, points);
  }
  return { width: x + NODE_W + PAD, height: middle + half + PAD, boxes, routes, headers };
}

/** Curve from a node's right side, straight along each lane, to the next node's left side. */
function edgePath(from: Box, lanes: ReadonlyArray<Box>, to: Box): string {
  let d = `M${from.x},${from.y}`;
  let at = from;
  const curve = (p: Box) => {
    const bend = Math.max(16, (p.x - at.x) / 2);
    d += ` C${at.x + bend},${at.y} ${p.x - bend},${p.y} ${p.x},${p.y}`;
  };
  for (const p of lanes) {
    curve(p);
    d += ` L${p.x + NODE_W},${p.y}`;
    at = { x: p.x + NODE_W, y: p.y };
  }
  curve({ x: to.x - 5, y: to.y });
  return d;
}

function tone(status: PlanNode["status"]): string {
  if (status === "completed") return "ok";
  if (status === "failed") return "bad";
  if (status === "stalled" || status === "suspended") return "attn";
  if (status === "running" || status === "verifying" || status === "candidate") return "run";
  if (status === "pruned") return "pruned";
  return "idle";
}

function StatusMark({ status }: { status: PlanNode["status"] }) {
  const t = tone(status);
  return (
    <svg className={`graph-mark ${t}`} width="18" height="18" viewBox="0 0 18 18" role="img" aria-label={NODE_STATUS[status]}>
      {t === "ok" || t === "bad" || t === "attn" ? <circle cx="9" cy="9" r="9" className="fill" /> : null}
      {t === "ok" && <path d="M5.4 9.3l2.4 2.4 4.8-4.9" className="glyph" />}
      {t === "bad" && <path d="M6.3 6.3l5.4 5.4M11.7 6.3l-5.4 5.4" className="glyph" />}
      {t === "attn" && <path d="M7.2 5.8v6.4M10.8 5.8v6.4" className="glyph" />}
      {t === "run" && (
        <>
          <circle cx="9" cy="9" r="7.5" className="track" />
          <path d="M9 1.5a7.5 7.5 0 0 1 7.5 7.5" className="arc" />
        </>
      )}
      {t === "idle" && <circle cx="9" cy="9" r="7.5" className="track" />}
      {t === "pruned" && (
        <>
          <circle cx="9" cy="9" r="7.5" className="track" />
          <path d="M4 14L14 4" className="track" />
        </>
      )}
    </svg>
  );
}

function GraphNode({
  node,
  trail,
  box,
  onHover,
}: {
  node: PlanNode;
  trail: Trail;
  box: Box;
  onHover: (id: string | null) => void;
}) {
  const tries = trail.attempts.filter((a) => a.plan_node_id === node.plan_node_id);
  const latest = tries[tries.length - 1];
  const spent = latest?.started_at ? duration(latest.started_at, latest.ended_at) : "";
  const meta = [NODE_KIND[node.kind], tries.length > 1 ? `试了 ${tries.length} 次` : "", spent].filter(Boolean).join(" · ");
  const props = {
    className: `graph-node ${tone(node.status)}`,
    style: { left: box.x, top: box.y, width: NODE_W, height: NODE_H },
    title: `${node.title}\n${NODE_STATUS[node.status]}`,
    onMouseEnter: () => onHover(node.plan_node_id),
    onMouseLeave: () => onHover(null),
    onFocus: () => onHover(node.plan_node_id),
    onBlur: () => onHover(null),
  };
  const body = (
    <>
      <StatusMark status={node.status} />
      <span className="graph-text">
        <span className="graph-title">{node.title}</span>
        <span className="graph-meta">{meta}</span>
      </span>
    </>
  );
  // A node opens its latest Attempt's trace; earlier tries are linked from there.
  return latest ? (
    <Link {...props} to={attemptPath(trail.workspaceId, trail.runId, latest.attempt_id)}>
      {body}
    </Link>
  ) : (
    <div {...props}>{body}</div>
  );
}

const clamp = (value: number, low: number, high: number) => Math.min(high, Math.max(low, value));

/** The execution graph in a bounded canvas: drag to pan, ⌘/Ctrl + scroll or pinch to zoom. */
export function PlanGraphView({ plan, trail }: { plan: PlanGraph; trail: Trail }) {
  const g = layout(plan);
  const viewport = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(1);
  const [expanded, setExpanded] = useState(false);
  const [hover, setHover] = useState<string | null>(null);
  // Fade the sides that have more graph beyond them.
  const [more, setMore] = useState({ left: false, right: false });
  function measure() {
    const el = viewport.current;
    if (!el) return;
    const left = el.scrollLeft > 1;
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
    setMore((m) => (m.left === left && m.right === right ? m : { left, right }));
  }
  useEffect(measure);
  const scaleRef = useRef(scale);
  scaleRef.current = scale;

  /** Zoom around a point of the viewport, keeping that point still. */
  function zoomTo(next: number, px?: number, py?: number) {
    const el = viewport.current;
    const from = scaleRef.current;
    const to = clamp(next, MIN_SCALE, MAX_SCALE);
    if (!el || to === from) return;
    const ax = px ?? el.clientWidth / 2;
    const ay = py ?? el.clientHeight / 2;
    const left = ((el.scrollLeft + ax) * to) / from - ax;
    const top = ((el.scrollTop + ay) * to) / from - ay;
    scaleRef.current = to;
    setScale(to);
    requestAnimationFrame(() => {
      el.scrollLeft = left;
      el.scrollTop = top;
    });
  }

  function fit() {
    const el = viewport.current;
    if (!el) return;
    const to = clamp(Math.min(el.clientWidth / g.width, el.clientHeight / g.height, 1), MIN_SCALE, 1);
    scaleRef.current = to;
    setScale(to);
    requestAnimationFrame(() => el.scrollTo({ left: 0, top: 0 }));
  }

  // Start at full size, showing the first node that is not finished.
  useLayoutEffect(() => {
    const el = viewport.current;
    if (!el) return;
    const current = plan.nodes.find((n) => n.status !== "completed" && n.status !== "pruned");
    const box = current ? g.boxes.get(current.plan_node_id) : undefined;
    if (box && box.x + NODE_W > el.clientWidth) el.scrollLeft = box.x - el.clientWidth / 3;
    // Only when a different plan is shown.
  }, [plan.plan_revision_id]);

  useEffect(() => {
    const el = viewport.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      // A plain wheel scrolls the page or the canvas; pinch and ⌘/Ctrl + wheel zoom.
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      zoomTo(scaleRef.current * Math.exp(-e.deltaY * 0.01), e.clientX - rect.left, e.clientY - rect.top);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  });

  useEffect(() => {
    if (!expanded) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setExpanded(false);
    };
    window.addEventListener("keydown", onKey);
    requestAnimationFrame(fit);
    return () => window.removeEventListener("keydown", onKey);
  }, [expanded]);

  // Drag the background to pan; touch already scrolls natively.
  const drag = useRef<{ x: number; y: number; left: number; top: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  function onPointerDown(e: ReactPointerEvent<HTMLDivElement>) {
    const el = viewport.current;
    if (!el || e.pointerType !== "mouse" || e.button !== 0) return;
    if ((e.target as Element).closest("a, button")) return;
    drag.current = { x: e.clientX, y: e.clientY, left: el.scrollLeft, top: el.scrollTop };
    el.setPointerCapture(e.pointerId);
    setDragging(true);
  }
  function onPointerMove(e: ReactPointerEvent<HTMLDivElement>) {
    const el = viewport.current;
    const start = drag.current;
    if (!el || !start) return;
    el.scrollLeft = start.left - (e.clientX - start.x);
    el.scrollTop = start.top - (e.clientY - start.y);
  }
  function onPointerUp() {
    drag.current = null;
    setDragging(false);
  }

  const byId = new Map(plan.nodes.map((n) => [n.plan_node_id, n]));
  const branchLabel = new Map(plan.branches.map((b) => [b.branch_id, b.label]));
  const linked = (e: PlanGraph["edges"][number]) => hover !== null && (e.source_node_id === hover || e.target_node_id === hover);
  const height = expanded ? undefined : Math.min(MAX_HEIGHT, Math.max(160, g.height * scale));

  return (
    <>
      {expanded && <div className="graph-scrim" onClick={() => setExpanded(false)} />}
      <div className={`graph-canvas${expanded ? " expanded" : ""}`} role="group" aria-label="执行图">
        <div
          ref={viewport}
          className={`graph-viewport${dragging ? " dragging" : ""}${more.left ? " more-left" : ""}${more.right ? " more-right" : ""}`}
          style={{ height }}
          onScroll={measure}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
        >
          <div className="graph-sizer" style={{ width: g.width * scale, height: g.height * scale }}>
            <div className="graph" style={{ width: g.width, height: g.height, transform: `scale(${scale})` }}>
              {g.headers.map((h) => (
                <div key={h.x} className="graph-header" style={{ left: h.x, top: PAD, width: h.width }} title={h.title}>
                  {h.title}
                </div>
              ))}
              <svg className="graph-edges" width={g.width} height={g.height} aria-hidden="true">
                {plan.edges.map((e) => {
                  const s = g.boxes.get(e.source_node_id);
                  const t = g.boxes.get(e.target_node_id);
                  if (!s || !t) return null;
                  const tx = t.x;
                  const ty = t.y + NODE_H / 2;
                  const path = edgePath({ x: s.x + NODE_W, y: s.y + NODE_H / 2 }, g.routes.get(e.edge_id) ?? [], { x: tx, y: ty });
                  const done = byId.get(e.source_node_id)?.status === "completed";
                  const dashed = e.edge_type === "exploration" || e.edge_type === "conditional";
                  const state = linked(e) ? " active" : hover !== null ? " dim" : done ? " done" : "";
                  return (
                    <g key={e.edge_id} className={`graph-edge${state}${dashed ? " dashed" : ""}`}>
                      <path className="line" d={path} />
                      <path className="arrow" d={`M${tx - 6},${ty - 4} L${tx - 6},${ty + 4} L${tx},${ty} Z`} />
                    </g>
                  );
                })}
              </svg>
              {plan.edges.map((e) => {
                const label = e.condition ?? (e.branch_id ? branchLabel.get(e.branch_id) : undefined);
                const s = g.boxes.get(e.source_node_id);
                const t = g.boxes.get(e.target_node_id);
                if (!label || !s || !t) return null;
                const first = g.routes.get(e.edge_id)?.[0] ?? { x: t.x, y: t.y + NODE_H / 2 };
                return (
                  <span
                    key={e.edge_id}
                    className="graph-label"
                    style={{ left: (s.x + NODE_W + first.x) / 2, top: (s.y + NODE_H / 2 + first.y) / 2 }}
                    title={label}
                  >
                    {label}
                  </span>
                );
              })}
              {plan.nodes.map((node) => {
                const box = g.boxes.get(node.plan_node_id);
                return box ? <GraphNode key={node.plan_node_id} node={node} trail={trail} box={box} onHover={setHover} /> : null;
              })}
            </div>
          </div>
        </div>
        <div className="graph-tools">
          <button type="button" className="graph-tool" onClick={() => zoomTo(scale / 1.25)} aria-label="缩小" title="缩小">
            −
          </button>
          <button type="button" className="graph-tool zoom" onClick={() => zoomTo(1)} title="原始大小">
            {Math.round(scale * 100)}%
          </button>
          <button type="button" className="graph-tool" onClick={() => zoomTo(scale * 1.25)} aria-label="放大" title="放大">
            +
          </button>
          <button type="button" className="graph-tool" onClick={fit} title="看全图">
            全图
          </button>
          <button
            type="button"
            className="graph-tool"
            onClick={() => setExpanded(!expanded)}
            aria-label={expanded ? "收起" : "全屏"}
            title={expanded ? "收起（Esc）" : "全屏"}
          >
            <Icon name={expanded ? "close" : "expand"} size={14} />
          </button>
        </div>
      </div>
    </>
  );
}
