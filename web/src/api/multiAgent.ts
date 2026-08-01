
import { apiGet, apiPost } from "./client";
import type {
  AgentBudgetProjection,
  AgentRoutingProjection,
  DelegationRunProjection,
  DelegationTaskProjection,
  MultiAgentSnapshot,
} from "../types/multiAgent";

type JsonObject = Record<string, unknown>;

function object(value: unknown): JsonObject {
  return value && typeof value === "object" ? value as JsonObject : {};
}

function string(value: unknown, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

function number(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function boolean(value: unknown, fallback = false) {
  return typeof value === "boolean" ? value : fallback;
}

function budget(value: unknown): AgentBudgetProjection | undefined {
  if (!value || typeof value !== "object") return undefined;
  const item = object(value);
  return {
    token_limit: number(item.token_limit) ?? number(item.max_tokens) ?? number(item.allocated_tokens),
    tokens_used: number(item.tokens_used) ?? number(item.used_tokens),
    time_limit_ms: number(item.time_limit_ms) ?? number(item.max_duration_ms),
    elapsed_ms: number(item.elapsed_ms) ?? number(item.duration_ms),
    spawn_limit: number(item.spawn_limit) ?? number(item.max_spawns),
    spawns_used: number(item.spawns_used) ?? number(item.spawn_count),
    concurrent_limit: number(item.concurrent_limit) ?? number(item.max_concurrent),
    active_workers: number(item.active_workers),
    exhausted: boolean(item.exhausted),
    reason: string(item.reason) || null,
    max_spawn_per_session: number(item.max_spawn_per_session),
    max_concurrent_subagents: number(item.max_concurrent_subagents),
    max_subagent_spawn_depth: number(item.max_subagent_spawn_depth),
    max_fanout_per_turn: number(item.max_fanout_per_turn),
    max_multi_agent_tasks: number(item.max_multi_agent_tasks),
    max_wave_fanout: number(item.max_wave_fanout),
  };
}

function routing(value: unknown): AgentRoutingProjection | undefined {
  if (!value || typeof value !== "object") return undefined;
  const item = object(value);
  return {
    topology: string(item.topology, "single"),
    reason_code: string(item.reason_code) || string(item.reason, "unspecified"),
    explanation: string(item.explanation) || string(item.detail),
    downgraded_from: string(item.downgraded_from) || null,
  };
}

function delegationRun(value: unknown): DelegationRunProjection {
  const item = object(value);
  return {
    id: string(item.id) || string(item.run_id, "unknown"),
    topology: string(item.topology, "delegation"),
    reason: string(item.reason) || string(item.explanation)
      || string(item.reason_code),
    status: string(item.status, "unknown"),
    phase: string(item.phase) || undefined,
    verification: item.verification && typeof item.verification === "object"
      ? object(item.verification)
      : null,
    created_at: string(item.created_at) || undefined,
    completed_at: string(item.completed_at) || null,
    required_count: number(item.required_count),
    completed_count: number(item.completed_count),
    failed_count: number(item.failed_count),
    retry_count: number(item.retry_count),
    budget: budget(item.budget ?? item.budget_json),
  };
}

function delegationTask(value: unknown): DelegationTaskProjection {
  const item = object(value);
  const dependencies = item.dependencies ?? item.depends_on ?? item.dependency_ids;
  return {
    id: string(item.id) || string(item.task_id, "unknown"),
    run_id: string(item.run_id) || string(item.delegation_run_id) || undefined,
    title: string(item.title) || string(item.goal)
      || string(item.description) || string(item.task_id, "Untitled task"),
    description: string(item.description) || string(item.prompt) || undefined,
    agent_name: string(item.agent_name) || string(item.agent_type)
      || string(item.assigned_agent, "unassigned"),
    child_session_id: string(item.child_session_id) || null,
    status: string(item.status, "unknown"),
    required: boolean(item.required, true),
    integration_status: string(item.integration_status) || undefined,
    integration_error: string(item.integration_error) || undefined,
    report: item.report && typeof item.report === "object" ? object(item.report) : null,
    dependencies: Array.isArray(dependencies) ? dependencies.map((entry) => String(entry)) : [],
    generation: number(item.generation),
    retry_count: number(item.retry_count),
    max_retries: number(item.max_retries),
    failure_category: string(item.failure_category) || null,
    failure_detail: string(item.failure_detail) || string(item.error) || null,
    evidence_status: string(item.evidence_status) || null,
    token_budget: number(item.token_budget),
    tokens_used: number(item.tokens_used),
    time_budget_ms: number(item.time_budget_ms),
    elapsed_ms: number(item.elapsed_ms),
    resource: item.resource && typeof item.resource === "object"
      ? object(item.resource) as DelegationTaskProjection["resource"]
      : undefined,
  };
}

export function normalizeMultiAgentSnapshot(value: MultiAgentSnapshot): MultiAgentSnapshot {
  const raw = value as MultiAgentSnapshot & JsonObject;
  const rawRuns = raw.delegation_runs ?? raw.runs;
  const rawTasks = raw.delegation_tasks ?? raw.tasks;
  // Phase 4: preserve resource governance state
  const resource = raw.resource as Record<string, unknown> | undefined;
  return {
    ...value,
    routing: routing(raw.routing ?? raw.routing_decision),
    delegation_runs: Array.isArray(rawRuns) ? rawRuns.map(delegationRun) : [],
    delegation_tasks: Array.isArray(rawTasks) ? rawTasks.map(delegationTask) : [],
    limits: budget(raw.limits ?? raw.budget),
    resource,
  };
}

export function getMultiAgentSnapshot(
  sessionId: string,
  signal?: AbortSignal,
): Promise<MultiAgentSnapshot> {
  return apiGet<MultiAgentSnapshot>(
    `/api/multi-agent/${encodeURIComponent(sessionId)}`,
    signal,
  ).then(normalizeMultiAgentSnapshot);
}

export interface AgentDefinitionProjection {
  name: string;
  description: string;
  intent: string;
  workspace_mode: string;
  allowed_subagents: string[];
}

export function getAgentDefinitions(signal?: AbortSignal): Promise<AgentDefinitionProjection[]> {
  return apiGet<{ definitions: AgentDefinitionProjection[] }>(
    "/api/multi-agent/catalog/definitions",
    signal,
  ).then((value) => value.definitions);
}

export function cancelDelegationTask(
  sessionId: string,
  taskId: string,
  detail = "User cancelled delegation task",
) {
  return apiPost<Record<string, unknown>>(
    `/api/multi-agent/${encodeURIComponent(sessionId)}/tasks/${encodeURIComponent(taskId)}/cancel`,
    { detail },
  );
}

export function retryDelegationTask(sessionId: string, taskId: string) {
  return apiPost<Record<string, unknown>>(
    `/api/multi-agent/${encodeURIComponent(sessionId)}/tasks/${encodeURIComponent(taskId)}/retry`,
  );
}

export interface DelegationRunDetail {
  run: Record<string, unknown>;
  tasks: Array<Record<string, unknown>>;
  replacement_tasks?: Array<Record<string, unknown>>;
  integration_outcomes?: Array<Record<string, unknown>>;
}

export function getDelegationRun(sessionId: string, runId: string, signal?: AbortSignal) {
  return apiGet<DelegationRunDetail>(
    `/api/multi-agent/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}`,
    signal,
  );
}

export function cancelDelegationRun(
  sessionId: string,
  runId: string,
  detail = "User cancelled delegation run",
) {
  return apiPost<Record<string, unknown>>(
    `/api/multi-agent/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/cancel`,
    { detail },
  );
}

export function resumeDelegationRun(sessionId: string, runId: string) {
  return apiPost<DelegationRunDetail>(
    `/api/multi-agent/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/resume`,
  );
}

export function integrateDelegationRun(
  sessionId: string,
  runId: string,
  decisions: Array<{ task_id: string; action: "apply" | "discard" | "retain"; expected_revision: string }>,
) {
  return apiPost<DelegationRunDetail>(
    `/api/multi-agent/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/integrate`,
    { decisions },
  );
}

export function verifyDelegationRun(sessionId: string, runId: string) {
  return apiPost<Record<string, unknown>>(
    `/api/multi-agent/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/verify`,
  );
}
