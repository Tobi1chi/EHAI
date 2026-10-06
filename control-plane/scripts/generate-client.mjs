import { readFile, writeFile, mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const projectDirectory = resolve(scriptDirectory, "..");
const schemaDirectory = resolve(projectDirectory, "..", "schemas", "v1");
const outputPath = resolve(projectDirectory, "src", "generated", "ehai-client.ts");
const documentNames = [
  "common.schema.json",
  "events.schema.json",
  "queries.schema.json",
  "commands.schema.json",
  "notes.schema.json",
  "project-configuration.schema.json",
  "workflows.schema.json",
  "connectors.schema.json",
  "routing.schema.json",
  "workspace-manager.schema.json",
];

const documents = new Map();
for (const name of documentNames) {
  documents.set(name, JSON.parse(await readFile(resolve(schemaDirectory, name), "utf8")));
}
const openapi = JSON.parse(
  await readFile(resolve(schemaDirectory, "http-api.openapi.json"), "utf8"),
);
const managerOpenapi = JSON.parse(
  await readFile(resolve(schemaDirectory, "workspace-manager.openapi.json"), "utf8"),
);
const managerOperations = new Set(
  Object.values(managerOpenapi.paths).flatMap((pathItem) =>
    Object.values(pathItem).map((operation) => operation.operationId),
  ),
);
for (const operationId of [
  "listWorkspaces", "registerWorkspace", "getWorkspace", "startWorkspace", "stopWorkspace",
  "getWorkspaceOverview", "getWorkspaceExecutionConfig",
]) {
  if (!managerOperations.has(operationId)) {
    throw new Error(`Manager OpenAPI operation is missing: ${operationId}`);
  }
}

const requiredOperations = [
  "listWorkflows", "startWorkflow", "listWorkflowRuns", "getWorkflowRun", "decideWorkflow",
  "listLifeTasks", "getLifeTask", "updateLifeTask", "createRoutine", "listRoutines",
  "getRoutine", "updateRoutine", "getRoutineScheduler",
  "getWorkflowExecution",
  "registerConnector", "listConnectors", "getConnector", "invokeConnector",
  "listConnectorCalls", "getConnectorCall",
  "reconcileConnectorCall",
  "createRoutingLab", "listRoutingLabs", "getRoutingLab", "submitRoutingRequest", "listRoutingRequests",
  "getRoutingRequest", "advanceRoutingLab", "resolveRoutingRequest", "recordRoutingFeedback",
  "proposeRoutingRecipe", "startRoutingReplay", "listRoutingReplays", "getRoutingReplay", "publishRoutingRecipe",
  "pauseRoutingRecipe", "getRoutingMetrics",
  "getPlannerCapacity",
  "createNote", "listNotes", "getNote", "addNoteMessage", "decideNote",
  "registerEventConsumer", "getEventConsumer", "readConsumerEvents", "acknowledgeConsumerEvents",
  "getConfigurationHost", "getProjectConfiguration", "getProjectConfigurationVersions",
  "configureProject", "getRunConfiguration",
  "listInbox",
  "getInboxItem",
  "listProjects",
  "getProject",
  "getRuntimeContext",
  "importPlan",
  "getPlanImportSchema",
  "create_project_api_v1_projects_post",
  "create_goal_api_v1_goals_post",
  "propose_plan_api_v1_plans_propose_post",
  "discuss_plan_api_v1_planning_discuss_post",
  "get_planning_conversation_api_v1_planning__conversation_id__get",
  "approve_plan_api_v1_plans_approve_post",
  "start_run_api_v1_runs_start_post",
  "proposeProcess",
  "reviewProcess",
  "applyProcess",
  "integrateRun",
  "decideHumanCheck",
  "get_run_interventions_api_v1_runs__run_id__interventions_get",
  "list_result_adoptions_api_v1_runs__run_id__adoptions_get",
  "replyIntervention",
  "get_run_api_v1_runs__run_id__get",
  "getRunPlan",
  "getProcessRevision",
  "getProcessDraft",
  "getProcessReview",
  "getProcessDraftReviews",
  "getRunProcessDrafts",
  "getRunResult",
  "getRunTrajectoryReviews",
  "suspendAttemptFromReview",
  "list_worker_profiles_api_v1_workers_profiles_get",
  "list_worker_endpoints_api_v1_workers_endpoints_get",
  "get_attempt_runtime_api_v1_attempts__attempt_id__runtime_get",
  "extend_attempt_deadline_api_v1_attempts__attempt_id__deadline_post",
  "cancel_attempt_api_v1_attempts__attempt_id__cancel_post",
  "event_stream_api_v1_events_stream_get",
];
const actualOperations = new Set(
  Object.values(openapi.paths).flatMap((pathItem) =>
    Object.values(pathItem)
      .map((operation) => operation.operationId)
      .filter((operationId) => typeof operationId === "string"),
  ),
);
for (const operationId of requiredOperations) {
  if (!actualOperations.has(operationId)) {
    throw new Error(`OpenAPI operation is missing: ${operationId}`);
  }
}

const definitions = new Map();
for (const [documentName, document] of documents) {
  for (const [name, schema] of Object.entries(document.$defs ?? {})) {
    if (definitions.has(name)) {
      if (schema.$ref?.endsWith(`#/$defs/${name}`)) continue;
      throw new Error(`duplicate schema definition ${name} in ${documentName}`);
    }
    definitions.set(name, schema);
  }
}

function referenceName(reference) {
  const marker = "#/$defs/";
  const index = reference.indexOf(marker);
  if (index < 0) {
    throw new Error(`unsupported schema reference: ${reference}`);
  }
  return reference.slice(index + marker.length);
}

function propertyName(name) {
  return /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(name) ? name : JSON.stringify(name);
}

function literal(value) {
  if (typeof value === "string") return JSON.stringify(value);
  if (value === null) return "null";
  if (typeof value === "boolean" || typeof value === "number") return String(value);
  throw new Error(`unsupported literal: ${JSON.stringify(value)}`);
}

function schemaType(schema) {
  if (schema === true) return "unknown";
  if (schema === false) return "never";
  if (schema.$ref) return referenceName(schema.$ref);
  if (Object.hasOwn(schema, "const")) return literal(schema.const);
  if (schema.enum) return schema.enum.map(literal).join(" | ");
  if (schema.oneOf) return schema.oneOf.map(schemaType).join(" | ");
  if (schema.anyOf) return schema.anyOf.map(schemaType).join(" | ");
  if (schema.allOf) return schema.allOf.map(schemaType).join(" & ");
  if (Array.isArray(schema.type)) {
    return schema.type.map((type) => schemaType({ ...schema, type })).join(" | ");
  }
  switch (schema.type) {
    case "string":
      return "string";
    case "integer":
    case "number":
      return "number";
    case "boolean":
      return "boolean";
    case "null":
      return "null";
    case "array":
      return `ReadonlyArray<${schemaType(schema.items ?? true)}>`;
    case "object": {
      const properties = schema.properties ?? {};
      const required = new Set(schema.required ?? []);
      const fields = Object.entries(properties).map(
        ([name, value]) =>
          `  readonly ${propertyName(name)}${required.has(name) ? "" : "?"}: ${schemaType(value)};`,
      );
      if (fields.length > 0) return `{\n${fields.join("\n")}\n}`;
      if (schema.additionalProperties && schema.additionalProperties !== true) {
        return `Readonly<Record<string, ${schemaType(schema.additionalProperties)}>>`;
      }
      return schema.additionalProperties === false
        ? "Readonly<Record<string, never>>"
        : "Readonly<Record<string, unknown>>";
    }
    default:
      if (schema.properties) return schemaType({ ...schema, type: "object" });
      return "unknown";
  }
}

const typeDeclarations = [...definitions.entries()]
  .map(([name, schema]) =>
    name === "JsonValue"
      ? "export type JsonValue = JsonScalar | ReadonlyArray<JsonValue> | { readonly [key: string]: JsonValue };"
      : `export type ${name} = ${schemaType(schema)};`,
  )
  .join("\n\n");

const clientSource = `// Generated from schemas/v1. Do not edit by hand.\n\n${typeDeclarations}

export class EhaiApiError extends Error {
  readonly status: number;
  readonly detail: ErrorResponse | null;

  constructor(status: number, detail: ErrorResponse | null) {
    super(detail?.error.message ?? \`EHAI request failed with status \${status}\`);
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
    this.baseUrl = baseUrl.replace(/\\/$/, "");
    this.fetcher = fetcher;
  }

  createProject(request: CreateProjectRequest): Promise<ProjectResponse> {
    return this.request("/projects", "POST", request);
  }

  listProjects(): Promise<ProjectListResponse> {
    return this.request("/projects", "GET");
  }

  listInbox(filters: { projectId?: string; runId?: string } = {}): Promise<InboxListResponse> {
    const query = new URLSearchParams();
    if (filters.projectId !== undefined) query.set("project_id", filters.projectId);
    if (filters.runId !== undefined) query.set("run_id", filters.runId);
    const suffix = query.toString();
    return this.request("/inbox" + (suffix ? "?" + suffix : ""), "GET");
  }

  getInboxItem(kind: InboxKind, requestId: string): Promise<InboxDetailResponse> {
    return this.request("/inbox/" + encodeURIComponent(kind) + "/" + encodeURIComponent(requestId), "GET");
  }

  getProject(projectId: string): Promise<ProjectDetailResponse> {
    return this.request("/projects/" + encodeURIComponent(projectId), "GET");
  }

  getRuntimeContext(): Promise<RuntimeContextResponse> {
    return this.request("/runtime/context", "GET");
  }

  getPlannerCapacity(): Promise<PlannerCapacityResponse> {
    return this.request("/planning/capacity", "GET");
  }

  listWorkflows(): Promise<{ data: WorkflowDefinition[] }> {
    return this.request("/workflows", "GET");
  }

  createRoutingLab(projectId: string, request: CreateRoutingLabRequest): Promise<{ data: RoutingLabView }> {
    return this.request("/projects/" + encodeURIComponent(projectId) + "/routing-labs", "POST", request);
  }

  listRoutingLabs(projectId: string): Promise<{ data: RoutingLabView[] }> {
    return this.request("/projects/" + encodeURIComponent(projectId) + "/routing-labs", "GET");
  }

  getRoutingLab(labId: string): Promise<{ data: RoutingLabView }> {
    return this.request("/routing-labs/" + encodeURIComponent(labId), "GET");
  }

  configureRoutingFallback(labId: string, request: ConfigureRoutingFallbackRequest): Promise<{ data: RoutingLabView }> {
    return this.request("/routing-labs/" + encodeURIComponent(labId) + "/fallback", "POST", request);
  }

  configureRoutingProject(labId: string, request: ConfigureRoutingProjectRequest): Promise<{ data: RoutingLabView }> {
    return this.request("/routing-labs/" + encodeURIComponent(labId) + "/change-project", "POST", request);
  }

  submitRoutingRequest(labId: string, request: SubmitRoutingRequest): Promise<{ data: RoutingRequest }> {
    return this.request("/routing-labs/" + encodeURIComponent(labId) + "/requests", "POST", request);
  }

  listRoutingRequests(labId: string): Promise<{ data: RoutingRequest[] }> {
    return this.request("/routing-labs/" + encodeURIComponent(labId) + "/requests", "GET");
  }

  getRoutingRequest(requestId: string): Promise<{ data: RoutingRequest }> {
    return this.request("/routing-requests/" + encodeURIComponent(requestId), "GET");
  }

  advanceRoutingLab(labId: string): Promise<{ data: RoutingLabMetrics }> {
    return this.request("/routing-labs/" + encodeURIComponent(labId) + "/advance", "POST", {});
  }

  resolveRoutingRequest(requestId: string, request: ResolveRoutingRequest): Promise<{ data: RoutingRequest }> {
    return this.request("/routing-requests/" + encodeURIComponent(requestId) + "/resolve", "POST", request);
  }

  recordRoutingFeedback(requestId: string, request: RoutingFeedbackRequest): Promise<{ data: RoutingRequest }> {
    return this.request("/routing-requests/" + encodeURIComponent(requestId) + "/feedback", "POST", request);
  }

  proposeRoutingRecipe(labId: string, request: ProposeRoutingRecipeRequest): Promise<{ data: RoutingRecipe }> {
    return this.request("/routing-labs/" + encodeURIComponent(labId) + "/candidates", "POST", request);
  }

  startRoutingReplay(labId: string, request: StartRoutingReplayRequest): Promise<{ data: RoutingReplay }> {
    return this.request("/routing-labs/" + encodeURIComponent(labId) + "/replays", "POST", request);
  }

  listRoutingReplays(labId: string): Promise<{ data: RoutingReplay[] }> {
    return this.request("/routing-labs/" + encodeURIComponent(labId) + "/replays", "GET");
  }

  getRoutingReplay(replayId: string): Promise<{ data: RoutingReplay }> {
    return this.request("/routing-replays/" + encodeURIComponent(replayId), "GET");
  }

  publishRoutingRecipe(recipeId: string, request: PublishRoutingRecipeRequest): Promise<{ data: RoutingRecipe }> {
    return this.request("/routing-recipes/" + encodeURIComponent(recipeId) + "/publish", "POST", request);
  }

  pauseRoutingRecipe(recipeId: string, request: PauseRoutingRecipeRequest): Promise<{ data: RoutingRecipe }> {
    return this.request("/routing-recipes/" + encodeURIComponent(recipeId) + "/pause", "POST", request);
  }

  getRoutingMetrics(labId: string): Promise<{ data: RoutingLabMetrics }> {
    return this.request("/routing-labs/" + encodeURIComponent(labId) + "/metrics", "GET");
  }

  registerConnector(projectId: string, request: RegisterConnectorRequest): Promise<{ data: ConnectorConnection }> {
    return this.request("/projects/" + encodeURIComponent(projectId) + "/connectors", "POST", request);
  }

  listConnectors(projectId: string): Promise<{ data: ConnectorConnection[] }> {
    return this.request("/projects/" + encodeURIComponent(projectId) + "/connectors", "GET");
  }

  getConnector(connectorId: string): Promise<{ data: ConnectorConnection }> {
    return this.request("/connectors/" + encodeURIComponent(connectorId), "GET");
  }

  invokeConnector(connectorId: string, request: InvokeConnectorRequest): Promise<{ data: ConnectorCall }> {
    return this.request("/connectors/" + encodeURIComponent(connectorId) + "/calls", "POST", request);
  }

  listConnectorCalls(connectorId: string): Promise<{ data: ConnectorCall[] }> {
    return this.request("/connectors/" + encodeURIComponent(connectorId) + "/calls", "GET");
  }

  getConnectorCall(callId: string): Promise<{ data: ConnectorCall }> {
    return this.request("/connector-calls/" + encodeURIComponent(callId), "GET");
  }

  reconcileConnectorCall(callId: string, request: ReconcileConnectorCallRequest): Promise<{ data: ConnectorCall }> {
    return this.request("/connector-calls/" + encodeURIComponent(callId) + "/reconcile", "POST", request);
  }

  startWorkflow(projectId: string, request: StartWorkflowRequest): Promise<{ data: WorkflowRun }> {
    return this.request("/projects/" + encodeURIComponent(projectId) + "/workflow-runs", "POST", request);
  }

  listWorkflowRuns(projectId: string): Promise<{ data: WorkflowRun[] }> {
    return this.request("/projects/" + encodeURIComponent(projectId) + "/workflow-runs", "GET");
  }

  getWorkflowRun(workflowRunId: string): Promise<{ data: WorkflowRun }> {
    return this.request("/workflow-runs/" + encodeURIComponent(workflowRunId), "GET");
  }

  getWorkflowExecution(workflowRunId: string): Promise<{ data: WorkflowExecutionView }> {
    return this.request("/workflow-runs/" + encodeURIComponent(workflowRunId) + "/execution", "GET");
  }

  decideWorkflow(workflowRunId: string, request: DecideWorkflowRequest): Promise<{ data: WorkflowRun }> {
    return this.request("/workflow-runs/" + encodeURIComponent(workflowRunId) + "/decisions", "POST", request);
  }

  listLifeTasks(projectId: string): Promise<{ data: LifeTask[] }> {
    return this.request("/projects/" + encodeURIComponent(projectId) + "/life-tasks", "GET");
  }

  getLifeTask(taskId: string): Promise<{ data: LifeTask }> {
    return this.request("/life-tasks/" + encodeURIComponent(taskId), "GET");
  }

  updateLifeTask(taskId: string, request: UpdateLifeTaskRequest): Promise<{ data: LifeTask }> {
    return this.request("/life-tasks/" + encodeURIComponent(taskId), "POST", request);
  }

  createRoutine(projectId: string, request: CreateRoutineRequest): Promise<{ data: LifeRoutine }> {
    return this.request("/projects/" + encodeURIComponent(projectId) + "/routines", "POST", request);
  }

  listRoutines(projectId: string): Promise<{ data: LifeRoutine[] }> {
    return this.request("/projects/" + encodeURIComponent(projectId) + "/routines", "GET");
  }

  getRoutine(routineId: string): Promise<{ data: LifeRoutine }> {
    return this.request("/routines/" + encodeURIComponent(routineId), "GET");
  }

  updateRoutine(routineId: string, request: UpdateRoutineRequest): Promise<{ data: LifeRoutine }> {
    return this.request("/routines/" + encodeURIComponent(routineId), "POST", request);
  }

  getRoutineScheduler(): Promise<{ data: RoutineSchedulerStatus }> {
    return this.request("/routines/scheduler", "GET");
  }

  createNote(request: CreateNoteRequest): Promise<NoteResponse> {
    return this.request("/notes", "POST", request);
  }

  listNotes(filters: { projectId?: string; goalId?: string; runId?: string; includeResolved?: boolean } = {}): Promise<NoteListResponse> {
    const query = new URLSearchParams();
    if (filters.projectId !== undefined) query.set("project_id", filters.projectId);
    if (filters.goalId !== undefined) query.set("goal_id", filters.goalId);
    if (filters.runId !== undefined) query.set("run_id", filters.runId);
    if (filters.includeResolved !== undefined) query.set("include_resolved", String(filters.includeResolved));
    return this.request("/notes?" + query.toString(), "GET");
  }

  getNote(noteId: string): Promise<NoteResponse> {
    return this.request("/notes/" + encodeURIComponent(noteId), "GET");
  }

  addNoteMessage(noteId: string, request: AddNoteMessageRequest): Promise<NoteResponse> {
    return this.request("/notes/" + encodeURIComponent(noteId) + "/messages", "POST", request);
  }

  decideNote(noteId: string, request: DecideNoteRequest): Promise<NoteResponse> {
    return this.request("/notes/" + encodeURIComponent(noteId) + "/decisions", "POST", request);
  }

  registerEventConsumer(request: RegisterEventConsumerRequest): Promise<EventConsumerResponse> {
    return this.request("/event-consumers", "POST", request);
  }

  getEventConsumer(consumerId: string): Promise<EventConsumerResponse> {
    return this.request("/event-consumers/" + encodeURIComponent(consumerId), "GET");
  }

  readConsumerEvents(consumerId: string, request: ReadEventConsumerBatchRequest = {}): Promise<EventConsumerBatchResponse> {
    return this.request("/event-consumers/" + encodeURIComponent(consumerId) + "/batches", "POST", request);
  }

  acknowledgeConsumerEvents(consumerId: string, request: AcknowledgeEventConsumerRequest): Promise<EventConsumerAcknowledgementResponse> {
    return this.request("/event-consumers/" + encodeURIComponent(consumerId) + "/ack", "POST", request);
  }

  getConfigurationHost(): Promise<HostConfigurationResponse> {
    return this.request("/project-configuration-host", "GET");
  }

  getProjectConfiguration(projectId: string): Promise<ProjectConfigurationResponse> {
    return this.request("/projects/" + encodeURIComponent(projectId) + "/configuration", "GET");
  }

  getProjectConfigurationVersions(projectId: string): Promise<ProjectConfigurationVersionsResponse> {
    return this.request("/projects/" + encodeURIComponent(projectId) + "/configuration/versions", "GET");
  }

  configureProject(projectId: string, request: UpdateProjectConfigurationRequest): Promise<UpdateProjectConfigurationResponse> {
    return this.request("/projects/" + encodeURIComponent(projectId) + "/configuration", "POST", request);
  }

  getRunConfiguration(runId: string): Promise<RunConfigurationResponse> {
    return this.request("/runs/" + encodeURIComponent(runId) + "/configuration", "GET");
  }

  createGoal(request: CreateGoalRequest): Promise<GoalResponse> {
    return this.request("/goals", "POST", request);
  }

  importPlan(request: ImportPlanRequest): Promise<PlanGraphResponse> {
    return this.request("/plans/import", "POST", request);
  }

  getPlanImportSchema(): Promise<{ data: Record<string, unknown> }> {
    return this.request("/plans/import-schema", "GET");
  }

  proposePlan(request: ProposePlanRequest): Promise<PlanGraphResponse> {
    return this.request("/plans/propose", "POST", request);
  }

  replanPlan(request: ReplanPlanRequest): Promise<PlanGraphResponse> {
    return this.request("/plans/replan", "POST", request);
  }

  discussPlan(request: DiscussPlanRequest): Promise<PlanningConversationResponse> {
    return this.request("/planning/discuss", "POST", request);
  }

  getPlanningConversation(conversationId: string): Promise<PlanningConversationResponse> {
    return this.request(\`/planning/\${encodeURIComponent(conversationId)}\`, "GET");
  }

  approvePlan(request: ApprovePlanRequest): Promise<PlanGraphResponse> {
    return this.request("/plans/approve", "POST", request);
  }

  startRun(request: StartRunRequest): Promise<RunResponse> {
    return this.request("/runs/start", "POST", request);
  }

  proposeProcess(request: ProposeProcessRequest): Promise<ProcessDraftAcceptedResponse> {
    return this.request("/commands/propose-process", "POST", request);
  }

  reviewProcess(request: ReviewProcessRequest): Promise<ProcessReviewAcceptedResponse> {
    return this.request("/commands/review-process", "POST", request);
  }

  applyProcess(request: ApplyProcessRequest): Promise<ProcessRevisionResponse> {
    return this.request("/commands/apply-process", "POST", request);
  }

  integrateRun(runId: string, request: IntegrateRunRequest): Promise<CodeIntegrationResponse> {
    return this.request("/runs/" + encodeURIComponent(runId) + "/integrate", "POST", request);
  }

  decideHumanCheck(
    checkRunId: string,
    request: DecideHumanCheckRequest,
  ): Promise<RunResponse> {
    return this.request(
      \`/check-runs/\${encodeURIComponent(checkRunId)}/decision\`,
      "POST",
      request,
    );
  }

  pauseRun(runId: string, request: PauseRunRequest): Promise<RunResponse> {
    return this.request(\`/runs/\${encodeURIComponent(runId)}/pause\`, "POST", request);
  }

  resumeRun(runId: string, request: ResumeRunRequest): Promise<RunResponse> {
    return this.request(\`/runs/\${encodeURIComponent(runId)}/resume\`, "POST", request);
  }

  cancelRun(runId: string, request: CancelRunRequest): Promise<RunResponse> {
    return this.request(\`/runs/\${encodeURIComponent(runId)}/cancel\`, "POST", request);
  }

  getRun(runId: string): Promise<RunResponse> {
    return this.request(\`/runs/\${encodeURIComponent(runId)}\`, "GET");
  }

  getRunInterventions(runId: string): Promise<InterventionListResponse> {
    return this.request(\`/runs/\${encodeURIComponent(runId)}/interventions\`, "GET");
  }

  getRunTrajectoryReviews(runId: string): Promise<TrajectoryReviewListResponse> {
    return this.request(\`/runs/\${encodeURIComponent(runId)}/trajectory-reviews\`, "GET");
  }

  suspendAttemptFromReview(attemptId: string, request: SuspendAttemptRequest): Promise<SuspendAttemptResponse> {
    return this.request(\`/attempts/\${encodeURIComponent(attemptId)}/suspend\`, "POST", request);
  }

  listResultAdoptions(runId: string): Promise<ResultAdoptionListResponse> {
    return this.request(\`/runs/\${encodeURIComponent(runId)}/adoptions\`, "GET");
  }

  replyIntervention(
    interventionId: string,
    request: ReplyInterventionRequest,
  ): Promise<RunResponse> {
    return this.request(
      \`/interventions/\${encodeURIComponent(interventionId)}/reply\`,
      "POST",
      request,
    );
  }

  getRunResult(runId: string): Promise<RunResultResponse> {
    return this.request(\`/runs/\${encodeURIComponent(runId)}/result\`, "GET");
  }

  getPlanGraph(planRevisionId: string): Promise<PlanGraphResponse> {
    return this.request(\`/plans/\${encodeURIComponent(planRevisionId)}\`, "GET");
  }

  getRunPlan(runId: string): Promise<PlanGraphResponse> {
    return this.request(\`/runs/\${encodeURIComponent(runId)}/plan\`, "GET");
  }

  getProcessRevision(processRevisionId: string): Promise<ProcessRevisionResponse> {
    return this.request(
      \`/process-revisions/\${encodeURIComponent(processRevisionId)}\`,
      "GET",
    );
  }

  getProcessDraft(draftId: string): Promise<ProcessDraftResponse> {
    return this.request(\`/process-drafts/\${encodeURIComponent(draftId)}\`, "GET");
  }

  getProcessReview(reviewId: string): Promise<ProcessReviewResponse> {
    return this.request(\`/process-reviews/\${encodeURIComponent(reviewId)}\`, "GET");
  }

  getProcessDraftReviews(draftId: string): Promise<ProcessDraftReviewsResponse> {
    return this.request(\`/process-drafts/\${encodeURIComponent(draftId)}/reviews\`, "GET");
  }

  getRunProcessDrafts(runId: string): Promise<RunProcessDraftsResponse> {
    return this.request(\`/runs/\${encodeURIComponent(runId)}/process-drafts\`, "GET");
  }

  getExecutionTrace(runId: string): Promise<ExecutionTraceResponse> {
    return this.request(\`/runs/\${encodeURIComponent(runId)}/trace\`, "GET");
  }

  listWorkerProfiles(): Promise<WorkerProfileListResponse> {
    return this.request("/workers/profiles", "GET");
  }

  listWorkerEndpoints(): Promise<WorkerEndpointListResponse> {
    return this.request("/workers/endpoints", "GET");
  }

  getAttemptRuntime(attemptId: string): Promise<AttemptRuntimeResponse> {
    return this.request(\`/attempts/\${encodeURIComponent(attemptId)}/runtime\`, "GET");
  }

  extendAttemptDeadline(
    attemptId: string,
    request: ExtendAttemptDeadlineRequest,
  ): Promise<AttemptRuntimeResponse> {
    return this.request(
      \`/attempts/\${encodeURIComponent(attemptId)}/deadline\`,
      "POST",
      request,
    );
  }

  cancelAttempt(attemptId: string, request: CancelAttemptRequest): Promise<RunResponse> {
    return this.request(
      \`/attempts/\${encodeURIComponent(attemptId)}/cancel\`,
      "POST",
      request,
    );
  }

  listEvents(afterEventId?: string, limit = 100): Promise<EventPageResponse> {
    const query = new URLSearchParams({ limit: String(limit) });
    if (afterEventId !== undefined) query.set("after_event_id", afterEventId);
    return this.request(\`/events?\${query.toString()}\`, "GET");
  }

  eventStreamUrl(afterEventId?: string): string {
    const url = new URL(\`\${this.baseUrl}/api/v1/events/stream\`);
    if (afterEventId !== undefined) url.searchParams.set("after_event_id", afterEventId);
    return url.toString();
  }

  private async request<T>(path: string, method: string, body?: unknown): Promise<T> {
    const init: RequestInit = { method };
    if (body !== undefined) {
      init.headers = { "content-type": "application/json" };
      init.body = JSON.stringify(body);
    }
    const response = await this.fetcher(\`\${this.baseUrl}/api/v1\${path}\`, init);
    const payload: unknown = await response.json();
    if (!response.ok) {
      throw new EhaiApiError(response.status, isErrorResponse(payload) ? payload : null);
    }
    return payload as T;
  }
}

export class EhaiWorkspaceManagerClient {
  readonly baseUrl: string;
  readonly fetcher: FetchLike;

  constructor(baseUrl: string, fetcher: FetchLike = globalThis.fetch.bind(globalThis)) {
    const url = new URL(baseUrl);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password ||
        url.search || url.hash || !["/", "/api/v1", "/api/v1/"].includes(url.pathname)) {
      throw new Error("Workspace manager requires an HTTP(S) origin without credentials");
    }
    this.baseUrl = url.origin;
    this.fetcher = fetcher;
  }

  listWorkspaces(): Promise<WorkspaceListResponse> {
    return this.request("/workspaces", "GET");
  }

  registerWorkspace(request: WorkspaceRegistration): Promise<WorkspaceResponse> {
    return this.request("/workspaces", "POST", request);
  }

  getWorkspace(workspaceId: string): Promise<WorkspaceResponse> {
    return this.request(this.workspacePath(workspaceId), "GET");
  }

  startWorkspace(workspaceId: string): Promise<WorkspaceResponse> {
    return this.request(this.workspacePath(workspaceId) + "/start", "POST", {});
  }

  stopWorkspace(workspaceId: string): Promise<WorkspaceResponse> {
    return this.request(this.workspacePath(workspaceId) + "/stop", "POST", {});
  }

  getWorkspaceExecutionConfig(workspaceId: string): Promise<WorkspaceExecutionConfigResponse> {
    return this.request(this.workspacePath(workspaceId) + "/execution-config", "GET");
  }

  getWorkspaceOverview(): Promise<WorkspaceOverviewResponse> {
    return this.request("/workspace-overview", "GET");
  }

  workspace(workspaceId: string): EhaiApiClient {
    return new EhaiApiClient(this.baseUrl + this.workspacePath(workspaceId), this.fetcher);
  }

  private workspacePath(workspaceId: string): string {
    if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(workspaceId)) {
      throw new Error("Invalid workspace_id");
    }
    return "/workspaces/" + workspaceId;
  }

  private async request<T>(path: string, method: string, body?: unknown): Promise<T> {
    const init: RequestInit = { method };
    if (body !== undefined) {
      init.headers = { "content-type": "application/json" };
      init.body = JSON.stringify(body);
    }
    const response = await this.fetcher(this.baseUrl + "/api/v1" + path, init);
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
`;

await mkdir(dirname(outputPath), { recursive: true });
await writeFile(outputPath, clientSource, "utf8");
