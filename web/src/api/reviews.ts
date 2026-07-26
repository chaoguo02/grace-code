import { apiDelete, apiGet, apiPost } from "./client";

export interface ReviewFinding {
  severity: "HIGH" | "MEDIUM" | "LOW";
  category: "bug" | "improvement" | "hypothesis";
  title: string;
  description: string;
  file_path?: string;
  line_start?: number;
  line_end?: number;
  code_snippet?: string;
  verification?: string;
  recommendation?: string;
  reported_by?: string[];
  corroboration_count?: number;
  evidence_status?: "verified" | "hypothesis" | "invalid";
  evidence_error?: string;
}

export interface ReviewTask {
  id: string;
  lens: string;
  title: string;
  status: string;
  child_session_id: string;
  result: Record<string, unknown>;
  error: string;
  started_at?: string | null;
  completed_at?: string | null;
  attempts: ReviewTaskAttempt[];
}

export interface ReviewTaskAttempt {
  id: string;
  attempt_number: number;
  status: string;
  child_session_id: string;
  result: Record<string, unknown>;
  error: string;
  started_at: string;
  completed_at?: string | null;
}

export interface ReviewJob {
  id: string;
  session_id: string;
  status: string;
  workspace_revision: string;
  head_commit: string;
  retry_of: string;
  snapshot_available: boolean;
  diff_hash: string;
  changed_files: string[];
  focus: string;
  result: {
    findings?: ReviewFinding[];
    finding_count?: number;
    invalid_findings?: ReviewFinding[];
    invalid_finding_count?: number;
    task_states?: Record<string, string>;
    total_tokens?: number;
    snapshot_current?: boolean;
    current_workspace_revision?: string;
  };
  error: string;
  tasks: ReviewTask[];
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
}

export function startMultiAgentReview(
  sessionId: string,
  focus = "",
  maxAgents = 3,
): Promise<ReviewJob> {
  return apiPost(
    `/api/reviews/sessions/${encodeURIComponent(sessionId)}`,
    { focus, max_agents: maxAgents },
  );
}

export function getLatestReview(sessionId: string): Promise<ReviewJob | null> {
  return apiGet(
    `/api/reviews/sessions/${encodeURIComponent(sessionId)}/latest`,
  );
}

export function getReview(jobId: string): Promise<ReviewJob> {
  return apiGet(`/api/reviews/${encodeURIComponent(jobId)}`);
}

export function cancelReview(jobId: string): Promise<ReviewJob> {
  return apiPost(`/api/reviews/${encodeURIComponent(jobId)}/cancel`);
}

export function retryReview(jobId: string): Promise<ReviewJob> {
  return apiPost(`/api/reviews/${encodeURIComponent(jobId)}/retry`);
}

export function retryReviewTask(
  jobId: string,
  taskId: string,
): Promise<ReviewJob> {
  return apiPost(
    `/api/reviews/${encodeURIComponent(jobId)}/tasks/${encodeURIComponent(taskId)}/retry`,
  );
}

export function releaseReviewSnapshot(jobId: string): Promise<ReviewJob> {
  return apiDelete(`/api/reviews/${encodeURIComponent(jobId)}/snapshot`);
}
