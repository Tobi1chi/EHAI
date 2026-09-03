// Generated from schemas/v1. Do not edit by hand.

export type Id = string;

export type IdInput = string;

export type UtcDateTime = string;

export type IdempotencyKey = string;

export type ErrorResponse = {
  readonly error: {
  readonly code: string;
  readonly message: string;
};
};

export type EventType = "ProjectCreated" | "GoalCreated" | "CompletionContractConfirmed" | "PlanRevisionProposed" | "PlanRevisionApproved" | "PlanNodeReadied" | "PlanNodeStarted" | "PlanNodeCandidateSubmitted" | "PlanNodeCompleted" | "PlanNodeFailed" | "PlanNodePruned" | "BranchSelected" | "BranchPruned" | "RunStarted" | "RunPaused" | "RunResumed" | "RunCompleted" | "RunFailed" | "RunCancelled" | "AttemptQueued" | "AttemptDispatched" | "AttemptBound" | "AttemptHeartbeatObserved" | "AttemptWaiting" | "AttemptDeadlineExtended" | "AttemptRetryScheduled" | "AttemptStarted" | "AttemptSucceeded" | "AttemptFailed" | "AttemptTimedOut" | "AttemptCancelled" | "AttemptInterrupted" | "DispatchWorkClaimed" | "EndpointHealthChanged" | "ProviderUsageRecorded" | "ArtifactCreated" | "CheckStarted" | "CheckPassed" | "CheckFailed" | "CheckInterrupted" | "GatePassed" | "GateFailed" | "CheckpointCreated" | "CheckpointRestored" | "WorkspacePreserved";

export type EventEnvelope = {
  readonly id: Id;
  readonly type: EventType;
  readonly occurred_at: UtcDateTime;
  readonly schema_version: 1;
  readonly run_id: Id | null;
  readonly correlation_id: Id;
  readonly payload: Readonly<Record<string, unknown>>;
};

export type StoredEventRecord = {
  readonly offset: number;
  readonly event: EventEnvelope;
};

export type RunStatus = "pending" | "running" | "paused" | "completed" | "failed" | "cancelled";

export type AttemptStatus = "pending" | "running" | "succeeded" | "failed" | "timed_out" | "cancelled" | "interrupted";

export type PlanRevisionStatus = "draft" | "approved";

export type PlanNodeKind = "work" | "fork" | "evaluator" | "merge";

export type PlanNodeStatus = "pending" | "ready" | "running" | "candidate" | "verifying" | "completed" | "failed" | "pruned";

export type EdgeType = "dependency" | "exploration" | "conditional" | "merge";

export type BranchStatus = "active" | "selected" | "pruned";

export type ArtifactKind = "candidate" | "evidence" | "log" | "patch" | "worker_output" | "check_output";

export type CheckKind = "command" | "artifact" | "semantic";

export type CheckRunStatus = "pending" | "running" | "completed" | "failed" | "timed_out" | "cancelled" | "interrupted";

export type NullableId = Id | null;

export type NullableUtcDateTime = UtcDateTime | null;

export type NullableString = string | null;

export type IdList = ReadonlyArray<Id>;

export type Run = {
  readonly run_id: Id;
  readonly goal_id: Id;
  readonly plan_revision_id: Id;
  readonly status: RunStatus;
  readonly created_at: UtcDateTime;
  readonly started_at: NullableUtcDateTime;
  readonly ended_at: NullableUtcDateTime;
  readonly status_reason: NullableString;
};

export type PlanNode = {
  readonly plan_node_id: Id;
  readonly title: string;
  readonly instruction: string;
  readonly kind: PlanNodeKind;
  readonly required_dependency_ids: IdList;
  readonly required_check_ids: IdList;
  readonly status: PlanNodeStatus;
};

export type Edge = {
  readonly edge_id: Id;
  readonly source_node_id: Id;
  readonly target_node_id: Id;
  readonly edge_type: EdgeType;
  readonly branch_id: NullableId;
  readonly condition: NullableString;
};

export type Branch = {
  readonly branch_id: Id;
  readonly label: string;
  readonly fork_node_id: Id;
  readonly node_ids: IdList;
  readonly merge_node_id: Id;
  readonly status: BranchStatus;
};

export type PlanGraph = {
  readonly plan_revision_id: Id;
  readonly goal_id: Id;
  readonly version: number;
  readonly completion_contract_id: Id;
  readonly completion_contract_version: number;
  readonly created_at: UtcDateTime;
  readonly status: PlanRevisionStatus;
  readonly approved_at: NullableUtcDateTime;
  readonly supersedes_plan_revision_id: NullableId;
  readonly nodes: ReadonlyArray<PlanNode>;
  readonly edges: ReadonlyArray<Edge>;
  readonly branches: ReadonlyArray<Branch>;
};

export type Attempt = {
  readonly attempt_id: Id;
  readonly run_id: Id;
  readonly plan_node_id: Id;
  readonly sequence: number;
  readonly status: AttemptStatus;
  readonly artifact_ids: IdList;
  readonly created_at: UtcDateTime;
  readonly started_at: NullableUtcDateTime;
  readonly ended_at: NullableUtcDateTime;
  readonly outcome_reason: NullableString;
};

export type Artifact = {
  readonly artifact_id: Id;
  readonly kind: ArtifactKind;
  readonly name: string;
  readonly media_type: string;
  readonly size_bytes: number;
  readonly sha256: string;
  readonly created_at: UtcDateTime;
  readonly run_id: NullableId;
  readonly plan_node_id: NullableId;
  readonly attempt_id: NullableId;
};

export type CheckSpec = {
  readonly check_id: Id;
  readonly name: string;
  readonly kind: CheckKind;
  readonly description: string;
  readonly required: boolean;
  readonly command_argv: ReadonlyArray<string>;
  readonly semantic_required_terms: ReadonlyArray<string>;
};

export type CheckResult = {
  readonly check_id: Id;
  readonly check_run_id: Id;
  readonly run_id: Id;
  readonly plan_node_id: Id;
  readonly attempt_id: Id;
  readonly passed: boolean;
  readonly evaluated_at: UtcDateTime;
  readonly evidence_artifact_ids: IdList;
  readonly output: NullableString;
  readonly failure_reason: NullableString;
};

export type CheckRun = {
  readonly check_run_id: Id;
  readonly run_id: Id;
  readonly plan_node_id: Id;
  readonly attempt_id: Id;
  readonly check_id: Id;
  readonly status: CheckRunStatus;
  readonly created_at: UtcDateTime;
  readonly started_at: NullableUtcDateTime;
  readonly ended_at: NullableUtcDateTime;
  readonly result: CheckResult | null;
  readonly failure_reason: NullableString;
};

export type GateDecision = {
  readonly gate_id: Id;
  readonly run_id: Id;
  readonly plan_node_id: Id;
  readonly attempt_id: Id;
  readonly passed: boolean;
  readonly evaluated_at: UtcDateTime;
  readonly required_check_ids: IdList;
  readonly failed_check_ids: IdList;
  readonly evidence_artifact_ids: IdList;
  readonly reason: NullableString;
};

export type BranchSelection = {
  readonly fork_node_id: Id;
  readonly branch_id: Id;
};

export type Checkpoint = {
  readonly checkpoint_id: Id;
  readonly plan_revision_id: Id;
  readonly run_id: Id;
  readonly event_offset: number;
  readonly gate_decision: GateDecision;
  readonly branch_selections: ReadonlyArray<BranchSelection>;
  readonly artifact_ids: IdList;
  readonly created_at: UtcDateTime;
};

export type ExecutionTrace = {
  readonly run: Run;
  readonly attempts: ReadonlyArray<Attempt>;
  readonly artifacts: ReadonlyArray<Artifact>;
  readonly check_runs: ReadonlyArray<CheckRun>;
  readonly checkpoints: ReadonlyArray<Checkpoint>;
  readonly events: ReadonlyArray<StoredEventRecord>;
};

export type EventPage = {
  readonly events: ReadonlyArray<StoredEventRecord>;
  readonly after_event_id: NullableId;
  readonly next_after_event_id: NullableId;
  readonly latest_offset: number;
  readonly has_more: boolean;
};

export type WorkerProfile = {
  readonly worker_profile_id: Id;
  readonly name: string;
  readonly kind: "builtin" | "codex_cli" | "codex_app_server";
  readonly model: string;
  readonly capabilities: ReadonlyArray<string>;
  readonly session_policy: "new" | "reuse" | "fork";
  readonly budget_ref: string | null;
  readonly priority: number;
};

export type WorkerEndpoint = {
  readonly worker_endpoint_id: Id;
  readonly name: string;
  readonly worker_kind: "builtin" | "codex_cli" | "codex_app_server";
  readonly endpoint_type: "in_process" | "command" | "address";
  readonly capacity: number;
  readonly status: "enabled" | "draining" | "disabled";
};

export type RuntimeHealth = {
  readonly status: "starting" | "healthy" | "degraded" | "failed" | "stopped";
  readonly loop_active: boolean;
  readonly consecutive_failures: number;
  readonly restart_count: number;
  readonly last_error: string | null;
};

export type AttemptRuntime = {
  readonly attempt_id: Id;
  readonly worker_profile_id: NullableId;
  readonly worker_endpoint_id: NullableId;
  readonly agent_session_ref_id: NullableId;
  readonly provider_session_id: string | null;
  readonly session_recoverable: boolean | null;
  readonly execution_kind: "builtin_turn" | "external_execution" | null;
  readonly provider_execution_id: string | null;
  readonly activity: "queued" | "running" | "waiting" | "stalled" | null;
  readonly event_cursor: string | null;
  readonly heartbeat_at: NullableUtcDateTime;
  readonly progress_at: NullableUtcDateTime;
  readonly deadline_at: NullableUtcDateTime;
  readonly lease_expires_at: NullableUtcDateTime;
  readonly queue_reason: string | null;
  readonly diagnostics: ReadonlyArray<string>;
};

export type WorkerRequest = {
  readonly worker_request_id: Id;
  readonly attempt_id: Id;
  readonly kind: "command_approval" | "file_change_approval" | "user_input" | "permission_approval";
  readonly summary: string;
  readonly status: "pending" | "resolved" | "declined";
};

export type WorkerProfileList = ReadonlyArray<WorkerProfile>;

export type WorkerEndpointList = ReadonlyArray<WorkerEndpoint>;

export type WorkerRequestList = ReadonlyArray<WorkerRequest>;

export type CheckSpecList = ReadonlyArray<CheckSpec>;

export type CheckRunList = ReadonlyArray<CheckRun>;

export type CheckpointList = ReadonlyArray<Checkpoint>;

export type ArtifactList = ReadonlyArray<Artifact>;

export type RunResponse = {
  readonly data: Run;
};

export type PlanGraphResponse = {
  readonly data: PlanGraph;
};

export type ExecutionTraceResponse = {
  readonly data: ExecutionTrace;
};

export type CheckSpecListResponse = {
  readonly data: CheckSpecList;
};

export type CheckRunListResponse = {
  readonly data: CheckRunList;
};

export type CheckpointListResponse = {
  readonly data: CheckpointList;
};

export type ArtifactListResponse = {
  readonly data: ArtifactList;
};

export type ArtifactResponse = {
  readonly data: Artifact;
};

export type EventPageResponse = {
  readonly data: EventPage;
};

export type WorkerProfileListResponse = {
  readonly data: WorkerProfileList;
};

export type WorkerEndpointListResponse = {
  readonly data: WorkerEndpointList;
};

export type RuntimeHealthResponse = {
  readonly data: RuntimeHealth;
};

export type AttemptRuntimeResponse = {
  readonly data: AttemptRuntime;
};

export type WorkerRequestListResponse = {
  readonly data: WorkerRequestList;
};

export type WorkerRequestResponse = {
  readonly data: WorkerRequest;
};

export type JsonScalar = boolean | number | number | string | null;

export type JsonValue = JsonScalar | ReadonlyArray<JsonValue> | { readonly [key: string]: JsonValue };

export type P1CompletionCriterion = "artifact:non-empty" | "command:exit-zero" | "semantic:required-terms";

export type Project = {
  readonly project_id: Id;
  readonly name: string;
  readonly created_at: UtcDateTime;
};

export type CompletionContract = {
  readonly completion_contract_id: Id;
  readonly goal_id: Id;
  readonly version: number;
  readonly criteria: ReadonlyArray<P1CompletionCriterion>;
  readonly required_check_ids: ReadonlyArray<Id>;
  readonly created_at: UtcDateTime;
  readonly confirmed_at: UtcDateTime | null;
  readonly supersedes_completion_contract_id: Id | null;
};

export type Goal = {
  readonly goal_id: Id;
  readonly project_id: Id;
  readonly objective: string;
  readonly created_at: UtcDateTime;
  readonly completion_contract: CompletionContract | null;
  readonly status: "open" | "satisfied";
  readonly final_gate_id: Id | null;
  readonly satisfied_at: UtcDateTime | null;
};

export type CreateProjectRequest = {
  readonly idempotency_key: IdempotencyKey;
  readonly name: string;
};

export type CreateGoalRequest = {
  readonly idempotency_key: IdempotencyKey;
  readonly project_id: IdInput;
  readonly objective: string;
};

export type ProposePlanRequest = {
  readonly idempotency_key: IdempotencyKey;
  readonly goal_id: IdInput;
  readonly criteria: ReadonlyArray<P1CompletionCriterion>;
};

export type ReplanPlanRequest = {
  readonly idempotency_key: IdempotencyKey;
  readonly base_plan_revision_id: IdInput;
  readonly criteria: ReadonlyArray<P1CompletionCriterion>;
};

export type ApprovePlanRequest = {
  readonly idempotency_key: IdempotencyKey;
  readonly plan_revision_id: IdInput;
  readonly completion_contract_id: IdInput;
};

export type StartRunRequest = {
  readonly idempotency_key: IdempotencyKey;
  readonly plan_revision_id: IdInput;
};

export type RunActionRequest = {
  readonly idempotency_key: IdempotencyKey;
};

export type PauseRunRequest = RunActionRequest;

export type ResumeRunRequest = RunActionRequest;

export type CancelRunRequest = {
  readonly idempotency_key: IdempotencyKey;
  readonly reason?: string | null;
};

export type ExtendAttemptDeadlineRequest = {
  readonly idempotency_key: IdempotencyKey;
  readonly deadline_at: UtcDateTime;
};

export type ResolveWorkerRequestRequest = {
  readonly idempotency_key: IdempotencyKey;
  readonly resolution: Readonly<Record<string, JsonValue>>;
};

export type DeclineWorkerRequestRequest = RunActionRequest;

export type CancelAttemptRequest = RunActionRequest;

export type DataResponse = {
  readonly data: JsonValue;
};

export type ProjectResponse = {
  readonly data: Project;
};

export type GoalResponse = {
  readonly data: Goal;
};

export class EhaiApiError extends Error {
  readonly status: number;
  readonly detail: ErrorResponse | null;

  constructor(status: number, detail: ErrorResponse | null) {
    super(detail?.error.message ?? `EHAI request failed with status ${status}`);
    this.name = "EhaiApiError";
    this.status = status;
    this.detail = detail;
  }
}

export type FetchLike = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export class EhaiApiClient {
  readonly baseUrl: string;
  readonly fetcher: FetchLike;

  constructor(baseUrl: string, fetcher: FetchLike = globalThis.fetch.bind(globalThis)) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.fetcher = fetcher;
  }

  createProject(request: CreateProjectRequest): Promise<ProjectResponse> {
    return this.request("/projects", "POST", request);
  }

  createGoal(request: CreateGoalRequest): Promise<GoalResponse> {
    return this.request("/goals", "POST", request);
  }

  proposePlan(request: ProposePlanRequest): Promise<PlanGraphResponse> {
    return this.request("/plans/propose", "POST", request);
  }

  replanPlan(request: ReplanPlanRequest): Promise<PlanGraphResponse> {
    return this.request("/plans/replan", "POST", request);
  }

  approvePlan(request: ApprovePlanRequest): Promise<PlanGraphResponse> {
    return this.request("/plans/approve", "POST", request);
  }

  startRun(request: StartRunRequest): Promise<RunResponse> {
    return this.request("/runs/start", "POST", request);
  }

  pauseRun(runId: string, request: PauseRunRequest): Promise<RunResponse> {
    return this.request(`/runs/${encodeURIComponent(runId)}/pause`, "POST", request);
  }

  resumeRun(runId: string, request: ResumeRunRequest): Promise<RunResponse> {
    return this.request(`/runs/${encodeURIComponent(runId)}/resume`, "POST", request);
  }

  cancelRun(runId: string, request: CancelRunRequest): Promise<RunResponse> {
    return this.request(`/runs/${encodeURIComponent(runId)}/cancel`, "POST", request);
  }

  getRun(runId: string): Promise<RunResponse> {
    return this.request(`/runs/${encodeURIComponent(runId)}`, "GET");
  }

  getPlanGraph(planRevisionId: string): Promise<PlanGraphResponse> {
    return this.request(`/plans/${encodeURIComponent(planRevisionId)}`, "GET");
  }

  getExecutionTrace(runId: string): Promise<ExecutionTraceResponse> {
    return this.request(`/runs/${encodeURIComponent(runId)}/trace`, "GET");
  }

  listWorkerProfiles(): Promise<WorkerProfileListResponse> {
    return this.request("/workers/profiles", "GET");
  }

  listWorkerEndpoints(): Promise<WorkerEndpointListResponse> {
    return this.request("/workers/endpoints", "GET");
  }

  getAttemptRuntime(attemptId: string): Promise<AttemptRuntimeResponse> {
    return this.request(`/attempts/${encodeURIComponent(attemptId)}/runtime`, "GET");
  }

  listWorkerRequests(attemptId: string): Promise<WorkerRequestListResponse> {
    return this.request(
      `/attempts/${encodeURIComponent(attemptId)}/worker-requests`,
      "GET",
    );
  }

  resolveWorkerRequest(
    workerRequestId: string,
    request: ResolveWorkerRequestRequest,
  ): Promise<WorkerRequestResponse> {
    return this.request(
      `/worker-requests/${encodeURIComponent(workerRequestId)}/resolve`,
      "POST",
      request,
    );
  }

  declineWorkerRequest(
    workerRequestId: string,
    request: DeclineWorkerRequestRequest,
  ): Promise<WorkerRequestResponse> {
    return this.request(
      `/worker-requests/${encodeURIComponent(workerRequestId)}/decline`,
      "POST",
      request,
    );
  }

  extendAttemptDeadline(
    attemptId: string,
    request: ExtendAttemptDeadlineRequest,
  ): Promise<AttemptRuntimeResponse> {
    return this.request(
      `/attempts/${encodeURIComponent(attemptId)}/deadline`,
      "POST",
      request,
    );
  }

  cancelAttempt(attemptId: string, request: CancelAttemptRequest): Promise<RunResponse> {
    return this.request(
      `/attempts/${encodeURIComponent(attemptId)}/cancel`,
      "POST",
      request,
    );
  }

  listEvents(afterEventId?: string, limit = 100): Promise<EventPageResponse> {
    const query = new URLSearchParams({ limit: String(limit) });
    if (afterEventId !== undefined) query.set("after_event_id", afterEventId);
    return this.request(`/events?${query.toString()}`, "GET");
  }

  eventStreamUrl(afterEventId?: string): string {
    const url = new URL(`${this.baseUrl}/api/v1/events/stream`);
    if (afterEventId !== undefined) url.searchParams.set("after_event_id", afterEventId);
    return url.toString();
  }

  private async request<T>(path: string, method: string, body?: unknown): Promise<T> {
    const init: RequestInit = { method };
    if (body !== undefined) {
      init.headers = { "content-type": "application/json" };
      init.body = JSON.stringify(body);
    }
    const response = await this.fetcher(`${this.baseUrl}/api/v1${path}`, init);
    const payload: unknown = await response.json();
    if (!response.ok) {
      throw new EhaiApiError(response.status, isErrorResponse(payload) ? payload : null);
    }
    return payload as T;
  }
}

function isErrorResponse(value: unknown): value is ErrorResponse {
  if (typeof value !== "object" || value === null || !("error" in value)) return false;
  const error = value.error;
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    typeof error.code === "string" &&
    "message" in error &&
    typeof error.message === "string"
  );
}
