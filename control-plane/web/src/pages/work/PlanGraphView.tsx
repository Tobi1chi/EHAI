import { useLayoutEffect, useRef, useState } from "react";
import type { Attempt, PlanGraph, PlanNode } from "../../lib/api";
import { NODE_KIND, NODE_STATUS } from "../../lib/labels";
import { Link } from "../../lib/router";
import { attemptPath } from "./AttemptPage";

export type Trail = { readonly workspaceId: string; readonly runId: string; readonly attempts: ReadonlyArray<Attempt> };

const NODE_H = 72;
const GAP_X = 16;
const GAP_Y = 36;
const PAD = 12;
const LABEL = 28;
const BAND_GAP = 24;
/** On wide screens phase titles get a left column; narrow screens put them on a row above. */
const GUTTER = 180;

/** A waypoint for an edge that skips layers; it keeps its own lane so the edge never runs behind a node. */
const LANE = 12;
const MIN_NODE = 160;

type Box = { x: number; y: number };
type Band = { title: string; top: number; bottom: number };
type Layout = {
  width: number;
  height: number;
  nodeWidth: number;
  gutter: number;
  boxes: Map<string, Box>;
  /** Waypoint centres (top of the lane) for each edge that skips layers. */
  routes: Map<string, Box[]>;
  bands: Band[];
};

const lane = (edgeId: string, k: number) => `lane:${edgeId}:${k}`;

/**
 * Layer by longest path from the roots, top to bottom. A phase starts below the
 * previous phase, so each phase is one band. Within a layer, nodes follow their parents.
 */
function layout(plan: PlanGraph, available: number): Layout {
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
  // The last layer of phase i (and everything before it); -1 before the first phase.
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
  const layers: string[][] = Array.from({ length: count }, () => []);
  ids.forEach((id) => layers[depth.get(id) ?? 0]?.push(id));
  // Ordering uses the lane above a node as its parent when an edge skips layers.
  const above = new Map<string, string[]>(ids.map((id) => [id, []]));
  for (const e of edges) {
    const from = depth.get(e.source_node_id) ?? 0;
    const to = depth.get(e.target_node_id) ?? 0;
    let previous = e.source_node_id;
    for (let d = from + 1; d < to; d += 1) {
      const id = lane(e.edge_id, d);
      layers[d]?.push(id);
      phaseOf.set(id, phaseOf.get(e.source_node_id) ?? 99);
      above.set(id, [previous]);
      previous = id;
    }
    if (to > from) above.get(e.target_node_id)?.push(previous);
  }
  const position = new Map<string, number>();
  for (const layer of layers) {
    const centre = (id: string) => {
      const ps = (above.get(id) ?? []).map((p) => position.get(p)).filter((v): v is number => v !== undefined);
      return ps.length ? ps.reduce((a, b) => a + b, 0) / ps.length : Infinity;
    };
    layer.sort((a, b) => (phaseOf.get(a) ?? 99) - (phaseOf.get(b) ?? 99) || centre(a) - centre(b) || ids.indexOf(a) - ids.indexOf(b));
    layer.forEach((id, i) => position.set(id, i - (layer.length - 1) / 2));
  }

  const isLane = (id: string) => id.startsWith("lane:");
  // Widest layer in nodes and lanes; the node width is what makes it fit.
  const fit = (room: number) =>
    Math.min(
      260,
      ...layers.map((layer) => {
        const lanes = layer.filter(isLane).length;
        const nodes = layer.length - lanes;
        return nodes ? Math.floor((room - 2 * PAD - (layer.length - 1) * GAP_X - lanes * LANE) / nodes) : 260;
      }),
    );
  // The left title column only when the widest layer still fits beside it.
  const gutter = plan.phases.length > 0 && fit(available - GUTTER) >= MIN_NODE ? GUTTER : 0;
  const nodeWidth = Math.max(140, fit(available - gutter));
  const widthOf = (layer: string[]) =>
    layer.reduce((sum, id) => sum + (isLane(id) ? LANE : nodeWidth), 0) + Math.max(0, layer.length - 1) * GAP_X;
  const width = Math.max(available, gutter + Math.max(0, ...layers.map(widthOf)) + 2 * PAD);

  const boxes = new Map<string, Box>();
  const lanes = new Map<string, Box>();
  const bands: Band[] = [];
  let y = 0;
  let previous: number | undefined;
  layers.forEach((layer, i) => {
    const real = layer.find((id) => !isLane(id));
    const phase = real === undefined ? previous : phaseOf.get(real);
    const opens = phase !== undefined && phase !== previous;
    if (i > 0) y += opens && previous !== undefined ? BAND_GAP : GAP_Y;
    if (opens) {
      bands.push({ title: plan.phases[phase]?.title ?? "", top: y, bottom: 0 });
      y += gutter ? PAD : LABEL;
    } else if (i === 0) {
      y += PAD;
    }
    let x = gutter + (width - gutter - widthOf(layer)) / 2;
    for (const id of layer) {
      if (isLane(id)) lanes.set(id, { x: x + LANE / 2, y });
      else boxes.set(id, { x, y });
      x += (isLane(id) ? LANE : nodeWidth) + GAP_X;
    }
    y += NODE_H;
    const next = layers[i + 1]?.find((id) => !isLane(id));
    const nextPhase = next === undefined ? undefined : phaseOf.get(next);
    if (phase !== undefined && nextPhase !== phase) {
      y += PAD;
      const band = bands[bands.length - 1];
      if (band) band.bottom = y;
    }
    previous = phase;
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
  return { width, height: y + PAD, nodeWidth, gutter, boxes, routes, bands };
}

/** Curve from a node's bottom, straight down each lane, to the next node's top. */
function edgePath(from: Box, lanes: ReadonlyArray<Box>, to: Box): string {
  let d = `M${from.x},${from.y}`;
  let at = from;
  const curve = (p: Box) => {
    const bend = Math.max(12, (p.y - at.y) / 2);
    d += ` C${at.x},${at.y + bend} ${p.x},${p.y - bend} ${p.x},${p.y}`;
  };
  for (const p of lanes) {
    curve(p);
    d += ` L${p.x},${p.y + NODE_H}`;
    at = { x: p.x, y: p.y + NODE_H };
  }
  curve({ x: to.x, y: to.y - 5 });
  return d;
}

function tone(status: PlanNode["status"]): string {
  if (status === "completed") return "ok";
  if (status === "failed") return "bad";
  if (status === "stalled" || status === "suspended") return "attn";
  if (status === "running" || status === "verifying" || status === "candidate") return "run";
  if (status === "pruned") return "pruned";
  return "";
}

function GraphNode({ node, trail, box, width }: { node: PlanNode; trail: Trail; box: Box; width: number }) {
  const tries = trail.attempts.filter((a) => a.plan_node_id === node.plan_node_id);
  const latest = tries[tries.length - 1];
  const t = tone(node.status);
  const style = { left: box.x, top: box.y, width, height: NODE_H };
  const body = (
    <>
      <span className="graph-title">{node.title}</span>
      <span className="graph-meta">
        <span className={`graph-dot ${t}`} />
        {NODE_STATUS[node.status]}
        {tries.length > 1 ? ` · 试了 ${tries.length} 次` : ""} · {NODE_KIND[node.kind]}
      </span>
    </>
  );
  // A node opens its latest Attempt's trace; earlier tries are linked from there.
  return latest ? (
    <Link
      className={`graph-node ${t}`}
      style={style}
      to={attemptPath(trail.workspaceId, trail.runId, latest.attempt_id)}
      title={`${node.title}\n点开看这一步的轨迹`}
    >
      {body}
    </Link>
  ) : (
    <div className={`graph-node ${t}`} style={style} title={node.title}>
      {body}
    </div>
  );
}

export function PlanGraphView({ plan, trail }: { plan: PlanGraph; trail: Trail }) {
  const scroller = useRef<HTMLDivElement>(null);
  const [available, setAvailable] = useState(720);
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setAvailable(el.clientWidth));
    observer.observe(el);
    setAvailable(el.clientWidth);
    return () => observer.disconnect();
  }, []);

  const g = layout(plan, available);
  const byId = new Map(plan.nodes.map((n) => [n.plan_node_id, n]));
  const branchLabel = new Map(plan.branches.map((b) => [b.branch_id, b.label]));
  return (
    <div className="graph-scroll" ref={scroller}>
      <div className="graph" style={{ width: g.width, height: g.height }}>
        {g.bands.map((band) => (
          <div key={band.top} className="graph-band" style={{ top: band.top, height: band.bottom - band.top }}>
            <div
              className={`graph-band-title${g.gutter ? " side" : ""}`}
              title={band.title}
              style={g.gutter ? { width: g.gutter } : undefined}
            >
              {band.title}
            </div>
          </div>
        ))}
        <svg className="graph-edges" width={g.width} height={g.height} aria-hidden="true">
          {plan.edges.map((e) => {
            const s = g.boxes.get(e.source_node_id);
            const t = g.boxes.get(e.target_node_id);
            if (!s || !t) return null;
            const tx = t.x + g.nodeWidth / 2;
            const ty = t.y;
            const path = edgePath({ x: s.x + g.nodeWidth / 2, y: s.y + NODE_H }, g.routes.get(e.edge_id) ?? [], { x: tx, y: ty });
            const done = byId.get(e.source_node_id)?.status === "completed";
            const dashed = e.edge_type === "exploration" || e.edge_type === "conditional";
            const cls = `${done ? " done" : ""}${dashed ? " dashed" : ""}`;
            return (
              <g key={e.edge_id}>
                <path className={`graph-edge${cls}`} d={path} />
                <path className={`graph-arrow${done ? " done" : ""}`} d={`M${tx - 4},${ty - 6} L${tx + 4},${ty - 6} L${tx},${ty} Z`} />
              </g>
            );
          })}
        </svg>
        {plan.edges.map((e) => {
          const label = e.condition ?? (e.branch_id ? branchLabel.get(e.branch_id) : undefined);
          const s = g.boxes.get(e.source_node_id);
          const t = g.boxes.get(e.target_node_id);
          if (!label || !s || !t) return null;
          const first = g.routes.get(e.edge_id)?.[0] ?? { x: t.x + g.nodeWidth / 2, y: t.y };
          return (
            <span
              key={e.edge_id}
              className="graph-label"
              style={{ left: (s.x + g.nodeWidth / 2 + first.x) / 2, top: (s.y + NODE_H + first.y) / 2 }}
              title={label}
            >
              {label}
            </span>
          );
        })}
        {plan.nodes.map((node) => {
          const box = g.boxes.get(node.plan_node_id);
          return box ? <GraphNode key={node.plan_node_id} node={node} trail={trail} box={box} width={g.nodeWidth} /> : null;
        })}
      </div>
    </div>
  );
}
